#!/usr/bin/env bash
# Bootstrap for https://github.com/vladislove1337-sfc/openflux-manager
# Installs the MENU only. OpenFlux itself is installed from menu option 1.
set -Eeuo pipefail

REPO="vladislove1337-sfc/openflux-manager"
BRANCH="main"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Ошибка: нужны права root. Выполни: curl -fsSL <ссылка>/bootstrap.sh | sudo bash" >&2
    exit 1
fi
for tool in curl python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Ошибка: отсутствует $tool. Установи: apt-get update && apt-get install -y curl python3" >&2
        exit 1
    fi
done

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
ARCHIVE="$TMP/source.zip"
URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"

echo "Загружаю OpenFlux Manager из https://github.com/${REPO} ..."
curl --fail --location --silent --show-error --retry 3 \
     --proto '=https' --tlsv1.2 "$URL" --output "$ARCHIVE"

python3 -m zipfile -e "$ARCHIVE" "$TMP"
SRC="$TMP/openflux-manager-${BRANCH}"
if [[ ! -f "$SRC/install.sh" || ! -f "$SRC/openflux_menu.py" ]]; then
    echo "Ошибка: в репозитории должны быть install.sh и openflux_menu.py в корне" >&2
    exit 1
fi

# The installer is invoked in its original repo directory; it uses sibling openflux_menu.py.
sh "$SRC/install.sh"
echo
echo "Установлено. Для запуска: openflux-menu"
echo "Пункт 1 внутри меню устанавливает сам OpenFlux."
