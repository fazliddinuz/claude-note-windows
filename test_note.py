import json
import os
import tempfile
import unittest

import install
import note


class CodexTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rollout-test.jsonl")
        rows = [
            {"timestamp": "2026-09-10T10:00:00Z", "type": "session_meta",
             "payload": {"id": "codex-session", "cwd": "E:\\work\\demo"}},
            {"timestamp": "2026-09-10T10:00:01Z", "type": "turn_context",
             "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol"}},
            {"timestamp": "2026-09-10T10:00:02Z", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Fix the parser"}]}},
            {"timestamp": "2026-09-10T10:00:03Z", "type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "exec",
                         "input": "pytest"}},
            {"timestamp": "2026-09-10T10:00:04Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "Parser fixed."}]}},
            {"timestamp": "2026-09-10T10:00:05Z", "type": "token_usage_record",
             "payload": {"turn_id": "turn-1", "turn_token_usage": {
                 "input_tokens": 100, "cached_input_tokens": 40,
                 "cache_write_input_tokens": 10, "output_tokens": 20,
                 "total_tokens": 120}}},
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_metadata_and_digest(self):
        cfg = dict(note.DEFAULTS)
        digest, users, first, end, more = note.parse_transcript(self.path, cfg)
        self.assertEqual(note.transcript_kind(self.path), "codex")
        self.assertEqual(note.session_id_of_transcript(self.path), "codex-session")
        self.assertEqual(note.cwd_of_transcript(self.path), "E:\\work\\demo")
        self.assertEqual(note.project_of(self.path), "work_demo")
        self.assertIn("[USER] Fix the parser", digest)
        self.assertIn("[CODEX] Parser fixed.", digest)
        self.assertIn("[TOOL] exec pytest", digest)
        self.assertEqual((users, first, end, more), (1, "Fix the parser", 6, False))

    def test_codex_usage_and_tools(self):
        usage, cost = note.session_usage(self.path)
        self.assertEqual(cost, 0.000666)
        self.assertEqual(usage["gpt-5.6-sol"]["in"], 50)
        self.assertEqual(usage["gpt-5.6-sol"]["cw5"], 10)
        self.assertEqual(usage["gpt-5.6-sol"]["cr"], 40)
        self.assertEqual(usage["gpt-5.6-sol"]["requests"], 1)
        tools, started, ended, tool_cost = note.tool_histogram(self.path)
        self.assertEqual(tools, {"exec": 1})
        self.assertEqual(started, "2026-09-10T10:00:00Z")
        self.assertEqual(ended, "2026-09-10T10:00:05Z")
        self.assertEqual(tool_cost, {"exec": 0.0})

    def test_openai_official_prices(self):
        self.assertEqual(note.price_of("gpt-6-astra")["in"], 10.0)
        self.assertEqual(note.price_of("gpt-5.6-sol")["cr"], 0.4)
        self.assertEqual(note.price_of("gpt-5.6-terra")["out"], 12.0)
        self.assertEqual(note.price_of("gpt-5.6-luna")["cw5"], 0.25)
        self.assertEqual(note.price_of("gpt-5.5")["out"], 30.0)

    def test_codex_individual_responses_not_cumulative_turn_total(self):
        path = os.path.join(self.tmp.name, "rollout-responses.jsonl")
        rows = [
            {"type": "turn_context", "payload": {
                "turn_id": "turn-1", "model": "gpt-5.6-sol"}},
            {"type": "token_usage_record", "payload": {
                "turn_id": "turn-1", "response_id": "r1",
                "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                          "cache_write_input_tokens": 10, "output_tokens": 20,
                          "total_tokens": 120},
                "turn_token_usage": {"input_tokens": 100,
                                     "cached_input_tokens": 40,
                                     "cache_write_input_tokens": 10,
                                     "output_tokens": 20,
                                     "total_tokens": 120}}},
            {"type": "token_usage_record", "payload": {
                "turn_id": "turn-1", "response_id": "r2",
                "usage": {"input_tokens": 200, "cached_input_tokens": 160,
                          "cache_write_input_tokens": 0, "output_tokens": 30,
                          "total_tokens": 230},
                "turn_token_usage": {"input_tokens": 300,
                                     "cached_input_tokens": 200,
                                     "cache_write_input_tokens": 10,
                                     "output_tokens": 50,
                                     "total_tokens": 350}}},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        usage, _ = note.session_usage(path)
        row = usage["gpt-5.6-sol"]
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["in"], 90)
        self.assertEqual(row["cr"], 200)
        self.assertEqual(row["cw5"], 10)
        self.assertEqual(row["out"], 50)

    def test_codex_is_labeled_in_report_and_knowledge_base(self):
        cfg = dict(note.DEFAULTS)
        cfg["vault"] = os.path.join(self.tmp.name, "vault")
        cfg["state"] = os.path.join(self.tmp.name, "state")
        row = note.build_report(
            cfg, "codex-session", "work_demo", "2026-09-10", "SessionEnd",
            self.path, 0, 6, {}, 0, 0, title="Parser", summary="Fixed")
        self.assertEqual(row["agent"], "codex")
        name = note.write_session_note(
            cfg, {"title": "Parser", "summary": "Fixed"}, "work_demo",
            "2026-09-10", "codex-session", [], 0, 0, agent="codex")
        path = note.session_note_path(cfg, "work_demo", name)
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("agent: codex", body)
        self.assertIn("**Agent:** Codex", body)


class AntigravityTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "antigravity-test.jsonl")
        rows = [
            {"timestamp": "2026-09-19T10:00:00Z", "type": "session_meta",
             "payload": {"id": "antigravity-session", "cwd": "E:\\work\\demo",
                         "originator": "antigravity", "model": "gemini-2.5-pro"}},
            {"timestamp": "2026-09-19T10:00:01Z", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Antigravity ni tekshir"}]}},
            {"timestamp": "2026-09-19T10:00:02Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "run_command",
                         "arguments": json.dumps({"CommandLine": "dir"})}},
            {"timestamp": "2026-09-19T10:00:03Z", "type": "response_item",
             "payload": {"type": "function_call_output", "output": "file1.txt\nfile2.txt"}},
            {"timestamp": "2026-09-19T10:00:04Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "Antigravity sozlandi."}]}},
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_antigravity_metadata_and_digest(self):
        cfg = dict(note.DEFAULTS)
        digest, users, first, end, more = note.parse_transcript(self.path, cfg)
        self.assertEqual(note.transcript_kind(self.path), "antigravity")
        self.assertEqual(note.session_id_of_transcript(self.path), "antigravity-session")
        self.assertEqual(note.cwd_of_transcript(self.path), "E:\\work\\demo")
        self.assertEqual(note.project_of(self.path), "work_demo")
        self.assertIn("[USER] Antigravity ni tekshir", digest)
        self.assertIn("[CODEX] Antigravity sozlandi.", digest)
        self.assertIn("[TOOL] run_command", digest)
        self.assertEqual((users, first, end, more), (1, "Antigravity ni tekshir", 5, False))

    def test_antigravity_labels(self):
        cfg = dict(note.DEFAULTS)
        cfg["vault"] = os.path.join(self.tmp.name, "vault")
        cfg["state"] = os.path.join(self.tmp.name, "state")
        row = note.build_report(
            cfg, "antigravity-session", "work_demo", "2026-09-19", "SessionEnd",
            self.path, 0, 5, {}, 0, 0, title="AG Test", summary="Done")
        self.assertEqual(row["agent"], "antigravity")
        name = note.write_session_note(
            cfg, {"title": "AG Test", "summary": "Done"}, "work_demo",
            "2026-09-19", "antigravity-session", [], 0, 0, agent="antigravity")
        path = note.session_note_path(cfg, "work_demo", name)
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("agent: antigravity", body)
        self.assertIn("**Agent:** Antigravity", body)

    def test_gemini_prices(self):
        self.assertEqual(note.price_of("gemini-2.5-pro")["in"], 1.25)
        self.assertEqual(note.price_of("gemini-2.5-pro")["out"], 5.0)
        self.assertEqual(note.price_of("gemini-2.5-flash")["in"], 0.075)
        self.assertEqual(note.price_of("gemini-unknown-model")["in"], 0.0)


class InstallerTests(unittest.TestCase):
    def test_install_hooks_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "hooks.json")
            original = {"hooks": {"SessionEnd": [{"hooks": [
                {"type": "command", "command": "other-tool"}]}]}}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(original, fh)
            install.install_hooks(path, "Test", 3)
            data = install.load_json(path)
            commands = [h["hooks"][0]["command"]
                        for h in data["hooks"]["SessionEnd"]]
            self.assertIn("other-tool", commands)
            self.assertTrue(any("note.py" in command for command in commands))
            self.assertIn("SessionStart", data["hooks"])
            self.assertIn("PreCompact", data["hooks"])

    def test_install_hooks_splits_session_start_into_parts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            install.install_hooks(path, "Test", 20, start_parts=3)
            install.install_hooks(path, "Test", 20, start_parts=3)   # qayta o'rnatish
            data = install.load_json(path)
            groups = data["hooks"]["SessionStart"]
            self.assertEqual(len(groups), 1)
            cmds = [h["command"] for h in groups[0]["hooks"]]
            self.assertEqual([c.rsplit(" ", 1)[-1] for c in cmds], ["1", "2", "3"])
            self.assertTrue(all("--session-start --part" in c for c in cmds))
            self.assertTrue(all(h["timeout"] == 20 for h in groups[0]["hooks"]))
            end = data["hooks"]["SessionEnd"][0]["hooks"]
            self.assertEqual(len(end), 1)
            self.assertNotIn("--part", end[0]["command"])


def _claude_row(rid, model="claude-opus-5-5", cr=1000, out=50):
    return {"type": "assistant", "requestId": rid,
            "message": {"id": "m-" + rid, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": out,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": cr}}}


class SubagentTests(unittest.TestCase):
    """Yordamchi agentlar <sessiya>/subagents/ da - alohida hisoblanadi."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main = os.path.join(self.tmp.name, "sess-1.jsonl")
        with open(self.main, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": "salom"}}) + "\n")
            fh.write(json.dumps(_claude_row("main-1")) + "\n")
        sub = os.path.join(self.tmp.name, "sess-1", "subagents")
        os.makedirs(sub)
        self.agent = os.path.join(sub, "agent-abc123.jsonl")
        with open(self.agent, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_claude_row("sub-1", cr=5000)) + "\n")
            fh.write(json.dumps(_claude_row("sub-2", cr=150000)) + "\n")
            # yarim yozilgan qator (agent hali ishlayapti) - sanalmasin
            fh.write(json.dumps(_claude_row("sub-3"))[:40])
        with open(os.path.join(sub, "agent-abc123.meta.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"agentType": "general-purpose",
                       "description": "22-yordamchi: trial"}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def test_subagent_files_counted_separately(self):
        _pm, main_cost = note.session_usage(self.main)
        pm, cost, agents, done = note.subagent_usage(self.main)
        _x, expect = note.session_usage(self.agent, 0, 2)
        self.assertGreater(cost, 0)
        self.assertEqual(cost, expect)
        self.assertEqual(pm["claude-opus-5-5"]["requests"], 2)
        self.assertEqual(done, {"agent-abc123.jsonl": 2})
        self.assertEqual(len(agents), 1)
        a = agents[0]
        self.assertEqual((a["name"], a["type"], a["requests"]),
                         ("22-yordamchi: trial", "general-purpose", 2))
        self.assertEqual(a["peak_context"], 150010)
        # asosiy transkript hisobiga yordamchi so'rovlari kirmaydi
        self.assertEqual(_pm["claude-opus-5-5"]["requests"], 1)

    def test_incremental_no_double_count(self):
        _pm, _c, _a, done = note.subagent_usage(self.main)
        pm, cost, agents, done2 = note.subagent_usage(self.main, done)
        self.assertEqual((pm, cost, agents), ({}, 0.0, []))
        # yarim qator tugadi - endi faqat o'sha bitta so'rov sanaladi
        with open(self.agent, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_claude_row("sub-3"))[40:] + "\n")
        pm, cost, agents, done3 = note.subagent_usage(self.main, done2)
        self.assertEqual(pm["claude-opus-5-5"]["requests"], 1)
        self.assertEqual(done3["agent-abc123.jsonl"], 3)

    def test_ledger_merge_and_work_cost(self):
        pm, cost, agents, _d = note.subagent_usage(self.main)
        rows = [
            {"session": "s", "ts": "1", "date": "2026-10-01", "title": "Asl",
             "session_cost_usd": 2.0, "cost_usd": 0.1, "subagent_cost_usd": 1.0,
             "subagent_usage": pm, "subagents": agents},
            {"session": "s", "ts": "2", "date": "2026-10-02",
             "session_cost_usd": 3.0, "cost_usd": 0.1, "subagent_cost_usd": 0.5,
             "subagents": [dict(agents[0], cost_usd=0.5, requests=1,
                                peak_context=1)]},
            {"session": "s", "ts": "3", "date": "2026-10-09",
             "kind": "subagent_backfill", "title": "Boshqa",
             "subagent_cost_usd": 0.25, "subagents": []},
        ]
        m = note.merge_ledger_by_session(rows)
        self.assertEqual(len(m), 1)
        r = m[0]
        self.assertAlmostEqual(note.work_cost(r), 6.75)
        self.assertAlmostEqual(r["subagent_cost_usd"], 1.75)
        # backfill qatori sanani, nomni va ishlov sonini o'zgartirmaydi
        self.assertEqual((r["date"], r["title"], r["runs"]),
                         ("2026-10-02", "Asl", 2))
        self.assertEqual(len(r["subagents"]), 1)
        self.assertEqual(r["subagents"][0]["requests"], 3)
        self.assertEqual(r["subagents"][0]["peak_context"], 150010)

    def test_codex_has_no_subagents(self):
        self.assertEqual(note.subagent_transcripts(None), [])


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(note.DEFAULTS)
        self.cfg["vault"] = os.path.join(self.tmp.name, "vault")
        self.cfg["state"] = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.cfg["vault"], "projects"))
        os.makedirs(self.cfg["state"])
        import hashlib
        items = [{"section": s, "text": " ".join(
                     hashlib.sha1(("%s%d%d" % (s, i, k)).encode()).hexdigest()[:9]
                     for k in range(30))}
                 for i in range(12) for s in note.BRIEF_SECTIONS]
        note.write_brief(self.cfg, "demo", items, "2026-10-05", "E:\\demo")
        self.path = note.brief_path(self.cfg, "demo")
        with open(self.path, encoding="utf-8") as fh:
            self.orig = fh.read()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cut_at_boundary_keeps_tables_and_headings_whole(self):
        t = "a\n## S\n- one\n| h | h |\n|---|---|\n| 1 | 2 |\n## T\n- two"
        self.assertEqual(note.cut_at_boundary(t, 30), "a\n## S\n- one")
        self.assertEqual(note.cut_at_boundary(t, 1000), t)

    def test_session_start_context_fits_limit(self):
        import contextlib, io, sys
        old_cu, old_fo = note.catch_up, note.flush_outbox
        note.catch_up = lambda *a, **k: 0
        note.flush_outbox = lambda *a, **k: 0
        old_rp = note.resolve_project
        note.resolve_project = lambda *a, **k: "demo"
        out = io.StringIO()
        try:
            sys.stdin = io.StringIO('{"cwd": "E:\\\\demo"}')
            with contextlib.redirect_stdout(out):
                note.cmd_session_start(self.cfg)
        finally:
            sys.stdin = sys.__stdin__
            note.catch_up, note.flush_outbox, note.resolve_project = old_cu, old_fo, old_rp
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx), note.SESSION_CONTEXT_MAX)
        self.assertIn("Брифинг давоми", ctx)
        self.assertIn(self.path, ctx)
        # muhimlik tartibi: Қоидалар, Тузоқлар butun bo'lim; Технологиялар qoladi
        self.assertLess(ctx.index("## Қоидалар"), ctx.index("## Тузоқлар"))
        self.assertNotIn("## Технологиялар", ctx)
        self.assertIn("Технологиялар", ctx[ctx.index("Брифинг давоми"):])

    def _run_start(self, part):
        import contextlib, io, sys
        old_cu, old_fo = note.catch_up, note.flush_outbox
        note.catch_up = lambda *a, **k: 0
        note.flush_outbox = lambda *a, **k: 0
        old_rp = note.resolve_project
        note.resolve_project = lambda *a, **k: "demo"
        out = io.StringIO()
        try:
            sys.stdin = io.StringIO('{"cwd": "E:\\\\demo"}')
            with contextlib.redirect_stdout(out):
                note.cmd_session_start(self.cfg, part)
        finally:
            sys.stdin = sys.__stdin__
            note.catch_up, note.flush_outbox, note.resolve_project = old_cu, old_fo, old_rp
        if not out.getvalue().strip():
            return ""
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_session_start_parts_cover_whole_brief(self):
        parts = [self._run_start(n) for n in range(1, note.BRIEF_PARTS + 1)]
        for c in parts:
            self.assertLessEqual(len(c), note.SESSION_CONTEXT_MAX)
        self.assertIn("олдинги сессиялардан тайёр брифинг", parts[0])
        self.assertIn("## Қоидалар", parts[0])
        self.assertIn("2/", parts[1])
        body = note.FRONT_RE.sub("", self.orig, count=1)
        joined = "\n".join(parts)
        missing = [ln for ln in body.split("\n") if ln.strip() and ln.strip() not in joined]
        self.assertEqual(missing, [])
        self.assertNotIn("Брифинг давоми", joined)

    def test_session_start_small_brief_uses_only_first_part(self):
        with open(self.path, "w", encoding="utf-8") as fh:   # write_brief qo'shib yozadi
            fh.write("---\ntitle: demo\n---\n\n# demo\n\n## Қоидалар\n- кичик қоида\n")
        self.assertIn("кичик қоида", self._run_start(1))
        self.assertEqual(self._run_start(2), "")
        self.assertEqual(self._run_start(3), "")

    def test_brief_parts_splits_huge_section_on_lines_and_reports_rest(self):
        lines = ["- қатор %04d " % i + "x" * 60 for i in range(400)]
        body = "# demo\n\n## Тузоқлар\n" + "\n".join(lines)
        texts = note.brief_parts(body, [4000, 4000, 4000], "B.md")
        self.assertTrue(all(len(t) <= 4000 for t in texts))
        self.assertIn("## Тузоқлар (давоми)", texts[1])
        self.assertIn("Брифинг давоми", texts[2])
        joined = "\n".join(texts)
        for ln in lines[:100]:
            self.assertIn(ln, joined)          # qator bo'linmaydi, boshi tushmaydi

    def _fake(self, sections):
        def run(prompt, cfg, source="claude"):
            return json.dumps({"sections": sections}, ensure_ascii=False), {"cost_usd": 0.01}
        return run

    def test_compact_archives_then_rewrites(self):
        good = {s: ["%s ixcham %d" % (s, i) + " y" * 80 for i in range(3)]
                for s in note.BRIEF_SECTIONS}
        old = note.run_synthesis
        note.run_synthesis = self._fake(good)
        try:
            st, before, after, cost = note.compact_brief(self.cfg, "demo", "2026-10-06")
        finally:
            note.run_synthesis = old
        self.assertEqual(st, "ok")
        self.assertLess(after, note.BRIEF_COMPACT_AT)
        with open(note.brief_full_path(self.cfg, "demo"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.orig)
        with open(self.path, encoding="utf-8") as fh:
            new = fh.read()
        self.assertIn("compacted: 2026-10-06", new)
        self.assertIn("ixcham 2", new)

    def test_mixed_case_rate_detects_broken_uzbek_letters(self):
        self.assertEqual(note.mixed_case_rate("фақат ҳар йўқ API Go"), 0)
        self.assertGreater(note.mixed_case_rate("фаҲат ҲаР йУҲ"), 100)

    def test_compact_rejects_answer_not_shorter(self):
        long = {s: ["z" * 4000] for s in note.BRIEF_SECTIONS}
        old = note.run_synthesis
        note.run_synthesis = self._fake(long)
        try:
            st, *_ = note.compact_brief(self.cfg, "demo")
            st2, *_ = note.compact_brief(self.cfg, "demo")   # qayta pul sarflamaydi
        finally:
            note.run_synthesis = old
        self.assertEqual(st, "xato")
        self.assertEqual(st2, "kutilmoqda")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.orig)

    def test_compact_skips_small_clean_and_fixes_small_broken(self):
        small = "---\nproject: demo\n---\n# demo\n\n## Қоидалар\n- фақат ҳар куни\n"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(small)
        calls = []
        old = note.run_synthesis
        note.run_synthesis = lambda p, c, source="claude": (calls.append(p) or "{}", {})
        try:
            st, *_ = note.compact_brief(self.cfg, "demo")
            self.assertEqual(st, "kerak emas")            # kichik va toza - tegilmaydi
            self.assertEqual(calls, [])
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(small.replace("фақат ҳар", "фаҲат ҲаР"))
            note.compact_brief(self.cfg, "demo")
            self.assertEqual(len(calls), 3)                # buzilgan - ishga tushadi
        finally:
            note.run_synthesis = old

    def test_compact_large_brief_waits_for_new_lines(self):
        # Ixchamlangan, lekin tabiiy ravishda katta brifing har sessiyada qayta
        # ixchamlanmaydi - faqat yetarli yangi qator qo'shilganda.
        note._compacted_lines(self.cfg, "demo",
                              [t for _s, t in note._brief_bullets(self.orig)])
        st, *_ = note.compact_brief(self.cfg, "demo", dry=True)
        self.assertEqual(st, "kerak emas")
        import hashlib
        items = [{"section": "Тузоқлар", "text": " ".join(
                     hashlib.md5(("t%d-%d" % (i, k)).encode()).hexdigest()[:11]
                     for k in range(30))}
                 for i in range(20)]
        note.write_brief(self.cfg, "demo", items, "2026-10-06", "E:\\demo")
        st, *_ = note.compact_brief(self.cfg, "demo", dry=True)
        self.assertEqual(st, "sinov")

    def test_compact_retries_broken_orthography(self):
        broken = {s: ["фаҲат ҲаР йУҲ ҳЖЖЖ ҚаҚаҚ %d" % i + " й" * 60 for i in range(3)]
                  for s in note.BRIEF_SECTIONS}
        clean = {s: ["фақат ҳар йўқ %d" % i + " й" * 60 for i in range(3)]
                 for s in note.BRIEF_SECTIONS}
        answers = iter([broken, clean])
        old = note.run_synthesis
        note.run_synthesis = lambda p, c, source="claude": (
            json.dumps({"sections": next(answers)}, ensure_ascii=False), {})
        try:
            st, *_ = note.compact_brief(self.cfg, "demo")
        finally:
            note.run_synthesis = old
        self.assertEqual(st, "ok")
        with open(self.path, encoding="utf-8") as fh:
            new = fh.read()
        self.assertNotIn("фаҲат", new)
        self.assertEqual(note.ortho_bad_words(new), [])

    def test_compact_from_full_uses_archive_and_strong_model(self):
        good = {s: ["%s ixcham %d" % (s, i) + " y" * 80 for i in range(3)]
                for s in note.BRIEF_SECTIONS}
        seen = {}

        def run(prompt, cfg, source="claude"):
            seen["model"], seen["prompt"] = cfg["model"], prompt
            return json.dumps({"sections": good}), {}
        old = note.run_synthesis
        note.run_synthesis = run
        try:
            note.compact_brief(self.cfg, "demo")
            st, *_ = note.compact_brief(self.cfg, "demo", force=True, from_full=True)
        finally:
            note.run_synthesis = old
        self.assertEqual(st, "ok")
        self.assertEqual(seen["model"], note.DEFAULTS["strong_model"])
        # ikkinchi ixchamlash arxivga oldingi ixcham qatorlarni qo'shmaydi
        with open(note.brief_full_path(self.cfg, "demo"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.orig)
        self.assertIn(self.orig.split("\n- ")[1][:40], seen["prompt"])  # arxivdan

    def test_ortho_repair_uses_vocab_and_skips_code(self):
        os.makedirs(os.path.join(self.cfg["vault"], "notes"), exist_ok=True)
        with open(os.path.join(self.cfg["vault"], "notes", "a.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("фақат ҳар йўқ алоҳида тўғри бўйича ВидНоменклатуры")
        note._ORTHO_VOCAB.clear()
        t = "Бу ҲаР куни алоҲида. фаҲат тУҲри бУйича `фаҲат` ВидНоменклатуры йУК"
        fixed, n_fix, n_left = note.ortho_repair(t, self.cfg)
        self.assertEqual(fixed, "Бу ҳар куни алоҳида. фақат тўғри бўйича `фаҲат` "
                                "ВидНоменклатуры йўқ")
        self.assertEqual((n_fix, n_left), (6, 0))
        self.assertEqual(note.ortho_bad_words("ВидНоменклатуры кВт `йУҲ` API"), [])
        self.assertFalse(note.ortho_ok("фаҲат ҲаР йУҲ"))
        self.assertTrue(note.ortho_ok("фақат ҳар йўқ"))
        note._ORTHO_VOCAB.clear()


if __name__ == "__main__":
    unittest.main()
