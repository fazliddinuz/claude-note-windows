#!/bin/bash
# claude-note — macOS uchun bir marta bosib o'rnatish.
#
# Finder'da IKKI MARTA BOSING. Boshqa hech narsa qilish shart emas.
#
# Agar macOS "ishonchsiz dastur" desa: fayl ustida o'ng tugma -> Open ->
# Open. Bu faqat birinchi safar so'raladi.

cd "$(dirname "$0")" || exit 1

printf '\n'
printf '   ============================================================\n'
printf '    claude-note  -  o'"'"'rnatish (macOS)\n'
printf '   ============================================================\n'

# Yuklab olingan arxivdagi karantin belgisi ba'zan ishga tushishga xalaqit
# beradi. Bor bo'lsa jimgina olib tashlaymiz.
xattr -d -r com.apple.quarantine . >/dev/null 2>&1

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    # macOS'dagi eski python2 emasligiga ishonch hosil qilamiz
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  printf '\n'
  printf '   Python 3 topilmadi.\n\n'
  printf '   Terminal ochib shuni ishga tushiring:\n\n'
  printf '       brew install python3\n\n'
  printf '   Homebrew yo'"'"'q bo'"'"'lsa, avval uni o'"'"'rnating:\n'
  printf '       https://brew.sh\n\n'
  printf '   Yoki: https://www.python.org/downloads/macos/\n\n'
  printf '   Keyin shu faylni qayta ikki marta bosing.\n\n'
  read -r -p "   Yopish uchun Enter bosing..." _
  exit 1
fi

"$PY" "$(pwd)/ornat.py" "$@"
STATUS=$?

printf '\n'
read -r -p "   Yopish uchun Enter bosing..." _
exit $STATUS
