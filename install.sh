#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Нужен root: sudo sh install.sh" >&2
  exit 1
fi
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -f "$DIR/openflux_menu.py" ] || { echo "Не найден openflux_menu.py" >&2; exit 1; }
python3 -m py_compile "$DIR/openflux_menu.py"
install -o root -g root -m 700 "$DIR/openflux_menu.py" /usr/local/sbin/openflux-menu
printf 'OpenFlux Manager установлен.\nЗапуск: sudo openflux-menu\nМеню не меняло серверную конфигурацию. Для установки OpenFlux выберите пункт 1.\n'
