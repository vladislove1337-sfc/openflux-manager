#!/usr/bin/env python3
"""OpenFlux Cups.online install + manage (Ubuntu 22.04/systemd)."""
import base64
import datetime as dt
import json
import os
import hashlib
import platform
import secrets
import urllib.parse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "2.2.0"
RELEASE_TAG = "v0.17.1"
RELEASE_ROOT = "https://github.com/OpenFluxTeam/OpenFlux/releases/download"
UNIT = Path("/etc/systemd/system/openflux-cups.service")
LAUNCH = Path("/usr/local/libexec/openflux-cups-launch")
DATA = Path("/var/lib/openflux")
LOG_MAX = 5000
DOWNLOAD_CACHE = Path("/var/cache/openflux-manager")
SERVICE = "openflux-cups.service"
ENV = Path("/etc/openflux/cups.env")
BACKUPS = Path("/etc/openflux/backups")
BINARY = Path("/opt/openflux/openflux")
KEY = Path("/opt/openflux/secret.txt")
SYNC_TIMER = "openflux-cups-sync.timer"
TOKEN_RE = re.compile(r"[A-Za-z0-9_+/-]+={0,2}\Z")


class MenuError(Exception):
    pass


def cmd(*args, timeout=20):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MenuError(f"Не удалось выполнить {' '.join(args)}: {e}") from e


def sysd(*args):
    result = cmd("systemctl", *args, timeout=40)
    if result.returncode:
        raise MenuError((result.stderr or result.stdout or "systemctl завершился с ошибкой").strip())
    return result.stdout.strip()


def property_value(name):
    result = cmd("systemctl", "show", f"--property={name}", "--value", SERVICE)
    return result.stdout.strip() if result.returncode == 0 else ""


def decode_rooms(token):
    if not token or len(token) > 32768 or not TOKEN_RE.fullmatch(token):
        raise MenuError("Неверный формат Base64 комнат")
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, UnicodeDecodeError) as e:
        raise MenuError(f"Список комнат не декодируется: {e}") from e
    if not isinstance(data, dict) or data.get("v") != 2:
        raise MenuError("Ожидается список Cups версии 2")
    rooms = data.get("rooms")
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 32 or len(rooms) != len(set(map(str, rooms))):
        raise MenuError("Некорректное количество или состав комнат")
    if not all(isinstance(room, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", room) for room in rooms):
        raise MenuError("Неверные идентификаторы комнат")
    return rooms


def read_token():
    if not ENV.is_file():
        raise MenuError(f"Нет файла {ENV}")
    content = ENV.read_text(encoding="utf-8")
    values = [line.split("=", 1)[1].strip().strip('"') for line in content.splitlines()
              if line.startswith("CUPS_URL=")]
    if len(values) != 1 or not values[0]:
        raise MenuError("В cups.env нет непустого CUPS_URL")
    decode_rooms(values[0])
    return values[0]


def backup_env():
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    if not ENV.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = BACKUPS / f"cups.env.{stamp}.bak"
    shutil.copyfile(ENV, target)
    os.chmod(target, 0o600)
    return target


def write_env(token, *, backup=True):
    if token:
        decode_rooms(token)
    if backup:
        backup_env()
    ENV.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".cups.", dir=ENV.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"CUPS_URL={token}\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, ENV)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def current_logs():
    """Read logs only from the active systemd invocation, never older runs."""
    invocation = property_value("InvocationID")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", invocation):
        raise MenuError("systemd не отдал InvocationID; не читаю старые журналы во избежание ошибки")
    output = cmd("journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}",
                 "-o", "cat", "--no-pager", "-n", str(LOG_MAX), timeout=25)
    if output.returncode:
        raise MenuError(f"journalctl: {output.stderr.strip()}")
    return output.stdout.splitlines()


def newest_room_list(lines):
    found = None
    for i, line in enumerate(lines):
        if "=== COPY THIS TO CLIENT ===" not in line:
            continue
        for candidate in lines[i + 1:i + 4]:
            token = candidate.strip()
            try:
                decode_rooms(token)
            except MenuError:
                continue
            found = token
            break
    return found


def room_health(lines):
    # Startup messages cannot establish whether rooms are alive now.
    summary = [line.strip() for line in lines if "Cups: зашли в " in line]
    missing = [line.strip() for line in lines if "GET room status 404" in line]
    recreated = [line.strip() for line in lines if "сохранённые комнаты закрыты" in line]
    return summary[-1:] + missing[-2:] + recreated[-1:]


def pause():
    input("\nEnter — в меню...")


def confirm(question):
    return input(f"{question} [да/нет]: ").strip().lower() in ("да", "д", "y", "yes")


def header():
    print("\033[2J\033[H", end="")
    print("=" * 58)
    print("  OPENFLUX | EXIT NODE | CUPS.ONLINE       manager v" + VERSION)
    print("=" * 58)
    a = cmd("systemctl", "is-active", SERVICE).stdout.strip()
    e = cmd("systemctl", "is-enabled", SERVICE).stdout.strip()
    print(f"  Служба: {a or 'неизвестно'}  |  автозапуск: {e or 'неизвестно'}")
    try:
        token = read_token()
        print(f"  Комнаты в cups.env: {len(decode_rooms(token))}  |  AES: отдельный ключ")
    except MenuError:
        print("  Комнаты в cups.env: нет корректного списка")
    print("-" * 58)


def show_status():
    print("\n=== Состояние выходной ноды ===")
    for prop in ("ActiveState", "SubState", "UnitFileState", "ActiveEnterTimestamp", "MainPID", "NRestarts"):
        print(f"{prop}: {property_value(prop) or '-'}")
    print(f"Бинарник: {'есть' if BINARY.is_file() else 'НЕТ'} ({BINARY})")
    print(f"AES-ключ: {'есть' if KEY.is_file() else 'НЕТ'} (значение не выводится)")
    try:
        lines = current_logs()
        print("\nПоследние сообщения Cups в текущем запуске:")
        for line in room_health(lines) or ["Данных пока нет"]:
            print("  " + line[:180])
        new = newest_room_list(lines)
        old = read_token()
        if new and new != old:
            print("\nВНИМАНИЕ: в журнале текущего запуска есть НОВЫЕ комнаты!")
            print("Используй пункт 3 для сохранения в cups.env.")
    except MenuError as e:
        print("Журнал:", e)
    print("\nПримечание: active и 8/8 при старте не доказывают работу VPN на iPhone.")


def show_rooms():
    token = read_token()
    rooms = decode_rooms(token)
    print(f"\nСохранено комнат: {len(rooms)}")
    for i, room in enumerate(rooms, 1):
        print(f" {i:2}. {room}")
    print("\nЭто идентификаторы комнат, НЕ AES-ключ.")
    if confirm("Показать полную строку Base64 для вставки в iPhone?"):
        print("\n" + token + "\n")
        print("Не публикуй эту строку вместе с секретным AES-ключом.")


def sync_rooms():
    lines = current_logs()
    latest = newest_room_list(lines)
    if not latest:
        print("\nВ текущем запуске OpenFlux новый список не создавал.")
        print("Значит, переносить из журнала нечего. Текущий cups.env не изменён.")
        return
    try:
        current = read_token()
    except MenuError:
        current = ""
    if latest == current:
        print("\nКомнаты из журнала уже совпадают с cups.env. Ничего делать не нужно.")
        return
    print(f"\nВ текущем запуске создано {len(decode_rooms(latest))} новых комнат.")
    if confirm("Сохранить их в cups.env? (без перезапуска службы)"):
        write_env(latest)
        print("\nСохранено. Теперь выбери пункт 2 и перенеси Base64 на iPhone.")
        print("Работающую выходную ноду НЕ перезапускали.")


def wait_room_token(seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            found = newest_room_list(current_logs())
            if found:
                return found
        except MenuError:
            pass
        time.sleep(1)
    return None


def new_rooms():
    print("\n=== Принудительная смена комнат ===")
    print("Это создаст новый набор комнат Cups, оборвёт текущий VPN")
    print("и потребует заменить Base64 в профиле iPhone.")
    print("AES-ключ НЕ изменится. Текущая конфигурация будет сохранена в бэкап.")
    if not confirm("Создать новые комнаты прямо сейчас?"):
        return
    if not is_managed_unit():
        raise MenuError("Сначала выполни пункт 1 и переведи службу на управляемый запуск. Старый ExecStart не поддерживает создание комнат без URL.")
    try:
        old = read_token()
    except MenuError:
        old = ""
    backup = backup_env()
    print(f"Резервная копия: {backup}")
    timer_was_active = cmd("systemctl", "is-active", "--quiet", SYNC_TIMER).returncode == 0
    if timer_was_active:
        sysd("stop", SYNC_TIMER)
    try:
        write_env("", backup=False)  # empty URL -> exit creates a fresh room list
        sysd("restart", SERVICE)
        print("Запрашиваю новый список из журнала текущего запуска...")
        token = wait_room_token()
        if token:
            print(f"Создано комнат: {len(decode_rooms(token))}")
            write_env(token, backup=False)
            print("Новые комнаты сохранены. Перезапускаю ноду с закреплённым CUPS_URL...")
            sysd("restart", SERVICE)
            time.sleep(2)
            print("Открой пункт 3 и перенеси Base64 на iPhone.")
            print("Статус:", property_value("ActiveState"))
        else:
            write_env(old, backup=False)
            print("ОШИБКА: за 25 секунд новый список не появился.")
            print("Предыдущий cups.env восстановлен, но нода уже запускалась без URL.")
            print("Проверь журнал, прежде чем повторять. Если старые комнаты закрыты, они не оживут.")
    except Exception:
        # Restore file to prevent a permanent empty CUPS_URL, but don't silently
        # restart the service: it may already be running with new rooms.
        write_env(old, backup=False)
        raise
    finally:
        if timer_was_active:
            try:
                sysd("start", SYNC_TIMER)
            except MenuError as e:
                print(f"Предупреждение о таймере: {e}")


def restart_and_sync():
    if not confirm("Перезапустить openflux-cups? Текущий VPN оборвётся"):
        return
    sysd("restart", SERVICE)
    print("Служба перезапущена. Сверяю комнаты...")
    # If old rooms are alive, no new token is printed. Wait only briefly.
    time.sleep(3)
    lines = current_logs()
    if any("0 из" in x and "Cups: зашли" in x for x in lines[-80:]):
        print("Старые комнаты недоступны. Ожидаю создания новых...")
        wait_room_token(25)
        lines = current_logs()
    token = newest_room_list(lines)
    if token:
        try:
            current = read_token()
        except MenuError:
            current = ""
        if token != current:
            write_env(token)
            print("ВНИМАНИЕ: старые комнаты закрыты; новые автоматически сохранены!")
            if is_managed_unit():
                sysd("restart", SERVICE)
                print("Нода повторно запущена с сохранённым новым URL.")
            print("Пункт 3: скопировать новый список на iPhone.")
        else:
            print("Комнаты совпадают с сохранёнными.")
    else:
        print("Нового списка пока нет. Проверь журнал, комнаты могут ещё создаваться.")
    print("Статус:", property_value("ActiveState") or "неизвестно")


def print_logs():
    logs = cmd("journalctl", "-u", SERVICE, "-n", "65", "--no-pager", "-o", "cat")
    if logs.returncode:
        raise MenuError(logs.stderr)
    print("\n=== Последние 65 строк (списки Base64 скрыты) ===\n")
    for line in logs.stdout.splitlines():
        line = re.sub(r"\beyJ[A-Za-z0-9+/=_-]{90,}\b", "<BASE64_КОМНАТ_СКРЫТ>", line)
        print(line[:240])


def service_control():
    print("\n1 — Запустить")
    print("2 — Остановить")
    print("3 — Включить автозапуск")
    print("4 — Отключить автозапуск (не остановит сейчас)")
    print("0 — Назад")
    choice = input("Выбор: ").strip()
    actions = {"1": ("start",), "2": ("stop",), "3": ("enable",), "4": ("disable",)}
    if choice in actions and confirm("Выполнить выбранное действие?"):
        sysd(*actions[choice], SERVICE)
        print("Готово.")


def backup_management():
    print("\n1 — Сохранить резервную копию cups.env")
    print("2 — Показать последние резервные копии")
    print("0 — Назад")
    choice = input("Выбор: ").strip()
    if choice == "1":
        print("Бэкап:", backup_env())
    elif choice == "2":
        if not BACKUPS.exists():
            print("Бэкапов пока нет")
            return
        for path in sorted(BACKUPS.glob("cups.env.*.bak"), reverse=True)[:15]:
            print("  " + path.name)
        print("Восстановление бэкапа намеренно не автоматизировано:")
        print("устаревшие комнаты могут быть уже удалены на Cups.online.")


def diagnostics():
    print("\n=== Диагностика ===")
    try:
        token = read_token()
        print(f"cups.env: OK, v2, {len(decode_rooms(token))} комнат")
        mode = oct(ENV.stat().st_mode & 0o777)
        print(f"Права cups.env: {mode}")
    except MenuError as e:
        print("cups.env:", e)
    if KEY.exists():
        mode = oct(KEY.stat().st_mode & 0o777)
        print(f"AES-ключ: существует, права {mode}, содержимое скрыто")
    else:
        print("AES-ключ: НЕ НАЙДЕН")
    print("Бинарник:", BINARY if BINARY.is_file() else "НЕ НАЙДЕН")
    print("Важное: проверка 8/8 — состояние входа при старте, не постоянный мониторинг.")
    try:
        lines = current_logs()
        for line in room_health(lines) or ["Нет отметок о комнатах в журнале текущего запуска"]:
            print("  " + line[:200])
        token = newest_room_list(lines)
        if token and token != read_token():
            print("!!! Новая строка комнат в журнале отличается от cups.env. Выбери пункт 4.")
    except MenuError as e:
        print("Журнал:", e)


def is_managed_unit():
    """Only our launcher supports missing CUPS_URL to create new rooms."""
    try:
        return LAUNCH.as_posix() in UNIT.read_text(encoding="utf-8") and LAUNCH.is_file()
    except OSError:
        return False


def atomic_text(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def curl_get(url, out):
    result = cmd("curl", "--fail", "--location", "--silent", "--show-error",
                 "--retry", "2", "--max-time", "180", "--output", str(out), url, timeout=210)
    if result.returncode:
        raise MenuError("Не удалось скачать с GitHub: " + (result.stderr or result.stdout).strip())
    if not out.is_file() or out.stat().st_size == 0:
        raise MenuError("Получен пустой файл с GitHub")


def sha256_file(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def download_core_resumable(url, asset, expected_sha256):
    """Download to persistent cache; curl retries resume partial transfers.

    The checksum is mandatory before the cached file can be installed.
    Interrupted runs retain the .part file for the next invocation.
    """
    cache_dir = DOWNLOAD_CACHE / RELEASE_TAG
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cache_dir, 0o700)
    part = cache_dir / (asset + ".part")

    if part.exists():
        if not part.is_file() or part.is_symlink():
            raise MenuError(f"Кэш скачивания не является обычным файлом: {part}")
        os.chmod(part, 0o600)
        if part.stat().st_size and sha256_file(part) == expected_sha256:
            print("Файл уже полностью скачан и прошёл SHA256, использую кэш.", flush=True)
            return part
        print(f"Найден недокачанный файл: {part.stat().st_size / (1024 * 1024):.1f} МиБ.", flush=True)
    else:
        part.touch(mode=0o600)

    # A full attempt may last 20 minutes on a slow VPS. Curl's standard
    # transfer meter is intentionally visible (percentage + current speed).
    # Low-speed detection avoids indefinitely stuck connections.
    for attempt in range(1, 5):
        offset = part.stat().st_size
        print(f"\nПопытка {attempt}/4; уже загружено: {offset / (1024 * 1024):.1f} МиБ", flush=True)
        print("Прогресс/скорость показывает curl. Ctrl+C сохранит недокачанный файл.", flush=True)
        args = [
            "curl", "--fail", "--location", "--show-error",
            "--connect-timeout", "20",
            "--speed-limit", "1024", "--speed-time", "90",
            "--max-time", "1200",
            "--continue-at", "-",
            "--output", str(part), url,
        ]
        try:
            completed = subprocess.run(args, timeout=1230, check=False)
            return_code = completed.returncode
        except subprocess.TimeoutExpired:
            return_code = 124
            print("Внешний лимит времени попытки превышен.", flush=True)
        except OSError as exc:
            raise MenuError(f"Не удалось запустить curl: {exc}") from exc

        # Verify even when curl reports a transfer error: the file may have
        # completed just before the connection was closed.
        if part.stat().st_size and sha256_file(part) == expected_sha256:
            print("\nSHA256: OK — файл скачан полностью.", flush=True)
            return part

        if return_code == 0:
            # A completed transfer with a bad hash is not safe to resume.
            # Recreate the partial file from zero; never install a bad hash.
            print("ВНИМАНИЕ: SHA256 не совпал, повреждённый кэш удалён.", flush=True)
            part.unlink()
            part.touch(mode=0o600)
        elif return_code in (22, 33):
            if return_code == 22:
                raise MenuError("GitHub вернул ошибку HTTP. Проверь адрес релиза и доступ к GitHub.")
            print("Сервер не поддержал докачку; следующая попытка начнётся заново.", flush=True)
            part.unlink()
            part.touch(mode=0o600)
        else:
            print(f"Ошибка curl (код {return_code}), сохранено {part.stat().st_size / (1024 * 1024):.1f} МиБ.", flush=True)

        if attempt < 4:
            time.sleep(3)

    raise MenuError(
        "Не удалось завершить проверенную загрузку за 4 попытки. "
        f"Недокачанный файл сохранён: {part}. "
        "Повтори пункт 1 — загрузка продолжится с сохранённого объёма."
    )


def architecture_asset():
    machine = platform.machine().lower()
    mapping = {"x86_64": "openflux-linux-amd64", "aarch64": "openflux-linux-arm64",
               "armv7l": "openflux-linux-arm"}
    if machine not in mapping:
        raise MenuError(f"Архитектура {machine} не поддерживается установщиком")
    return mapping[machine]


def download_verified_core():
    """Release + checksum manifest from official GitHub release (not a signature)."""
    asset = architecture_asset()
    url = f"{RELEASE_ROOT}/{RELEASE_TAG}"
    with tempfile.TemporaryDirectory(prefix="openflux-download-") as td:
        td = Path(td)
        sums = td / "SHA256SUMS.txt"
        print(f"Скачиваю официальный OpenFlux {RELEASE_TAG} ({asset})")
        curl_get(f"{url}/SHA256SUMS.txt", sums)
        digest = None
        for line in sums.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*([a-fA-F0-9]{64})\s+\*?(\S+)\s*", line)
            if match and Path(match.group(2)).name == asset:
                digest = match.group(1).lower()
                break
        if not digest:
            raise MenuError(f"В официальном SHA256SUMS.txt нет хеша для {asset}. Установка остановлена.")
        target = download_core_resumable(f"{url}/{asset}", asset, digest)
        if sha256_file(target) != digest:
            raise MenuError("SHA256 НЕ СОВПАЛ: установка остановлена.")
        print("SHA256: OK (сверен с манифестом этого же релиза)")
        BINARY.parent.mkdir(parents=True, exist_ok=True)
        if BINARY.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = BINARY.with_name(f"openflux.backup-{stamp}")
            shutil.copy2(BINARY, backup)
            print(f"Старый бинарник сохранён: {backup}")
        # Do not mutate an executing binary in-place.
        destination = BINARY.with_name(".openflux-new")
        try:
            shutil.copyfile(target, destination)
            os.chmod(destination, 0o755)
            os.replace(destination, BINARY)
        finally:
            destination.unlink(missing_ok=True)
        print(f"OpenFlux {RELEASE_TAG} установлен в {BINARY}")


def runtime_user():
    import pwd
    import grp
    try:
        account = pwd.getpwnam("openflux")
    except KeyError:
        result = cmd("useradd", "--system", "--user-group", "--home-dir", str(DATA),
                     "--shell", "/usr/sbin/nologin", "openflux")
        if result.returncode:
            raise MenuError("Не удалось создать пользователя openflux: " + result.stderr.strip())
        account = pwd.getpwnam("openflux")
    try:
        group = grp.getgrnam("openflux")
    except KeyError as e:
        raise MenuError("Нет группы openflux; проверь системного пользователя") from e
    DATA.mkdir(parents=True, exist_ok=True)
    os.chown(DATA, account.pw_uid, group.gr_gid)
    os.chmod(DATA, 0o750)
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    # The non-root openflux account needs traverse access to the binary/key.
    os.chown(BINARY.parent, 0, group.gr_gid)
    os.chmod(BINARY.parent, 0o750)
    KEY.parent.mkdir(parents=True, exist_ok=True)
    if not KEY.exists():
        atomic_text(KEY, secrets.token_hex(32) + "\n", 0o640)
        print("Создан новый AES-256 ключ. См. пункт 8, чтобы перенести его на iPhone.")
    else:
        if not KEY.is_file() or not KEY.stat().st_size:
            raise MenuError("Файл AES-ключа существует, но пустой или не обычный файл")
        print("Существующий AES-ключ сохранён без изменений.")
    os.chown(KEY, 0, group.gr_gid)
    os.chmod(KEY, 0o640)
    ENV.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(ENV.parent, 0o700)
    if not ENV.exists():
        atomic_text(ENV, "CUPS_URL=\n", 0o600)
        print("Подготовлена пустая конфигурация комнат (пункт 2 создаст комнаты).")
    else:
        print("Существующий список комнат не изменён.")


def managed_launcher():
    return '''#!/bin/sh
set -eu
set -- /opt/openflux/openflux --role=exit --mode=l4 --transport=cupsonline \\
  --encryption-key-file=/opt/openflux/secret.txt \\
  --cookie-store=/var/lib/openflux/cookies-cupsonline.json
if [ -n "${CUPS_URL:-}" ]; then
  set -- "$@" "--url=$CUPS_URL"
fi
exec "$@"
'''


def managed_service():
    return '''[Unit]
Description=OpenFlux Cups.online Exit Node (Manager)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=openflux
Group=openflux
WorkingDirectory=/var/lib/openflux
EnvironmentFile=-/etc/openflux/cups.env
ExecStart=/usr/local/libexec/openflux-cups-launch
Restart=always
RestartSec=10
UMask=0077
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''


def install_unit_if_allowed():
    if UNIT.exists() and not is_managed_unit():
        print("\nНайдена существующая служба openflux-cups с прежним ExecStart.")
        print("Для генерации новых комнат нужно перейти на запуск через обёртку,")
        print("которая позволяет запускать OpenFlux без --url при пустом CUPS_URL.")
        print("Текущие ключ, комнаты и настройки подключения будут сохранены.")
        if not confirm("Создать резервную копию и заменить описание службы?"):
            print("Службу не изменяли; пункт 2 пока недоступен.")
            return False
        BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUPS / f"openflux-cups.service.{stamp}.bak"
        shutil.copy2(UNIT, backup)
        os.chmod(backup, 0o600)
        print("Копия прежней службы:", backup)
    LAUNCH.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(LAUNCH, managed_launcher(), 0o755)
    if not UNIT.exists() or not is_managed_unit() or UNIT.read_text() != managed_service():
        atomic_text(UNIT, managed_service(), 0o644)
    sysd("daemon-reload")
    sysd("enable", SERVICE)
    print("Управляемая служба установлена и включена в автозагрузку.")
    if property_value("ActiveState") == "active":
        print("Работающий OpenFlux не перезапускали: новое описание вступит в силу при следующем запуске.")
    else:
        print("Нода пока не запущена. Нажми пункт 2 для создания комнат и запуска.")
    return True


def install_core_and_service():
    print("\n=== 1. Установка и подготовка OpenFlux ===")
    print("Источник: OpenFluxTeam/OpenFlux, стабильный релиз", RELEASE_TAG)
    print("Бинарник:", "найден" if BINARY.is_file() else "отсутствует")
    print("AES-ключ:", "найден" if KEY.is_file() else "отсутствует")
    print("Служба:", "найдена" if UNIT.exists() else "отсутствует")
    if not BINARY.is_file():
        if not confirm(f"Скачать и установить OpenFlux {RELEASE_TAG} с проверкой SHA256?"):
            return
        download_verified_core()
    else:
        print("\nБинарник уже существует. По умолчанию оставляем установленную версию.")
        if confirm(f"Заменить бинарник официальной версией {RELEASE_TAG}? Текущая будет сохранена"):
            download_verified_core()
            if property_value("ActiveState") == "active":
                print("Важно: процесс продолжает использовать старый бинарник до перезапуска.")
    runtime_user()
    install_unit_if_allowed()


def show_key():
    print("\n=== AES-ключ для iPhone ===")
    if not KEY.is_file():
        raise MenuError("Ключ отсутствует. Сначала установи OpenFlux (пункт 1).")
    print("Это секрет шифрования, не путай с Base64-списком комнат.")
    print("Не отправляй ключ посторонним и не публикуй скриншот.")
    if confirm("Показать секретный AES-ключ в терминале?"):
        print("\n" + KEY.read_text(encoding="utf-8").strip() + "\n")


def backups_and_diagnostics():
    print("\n1 — Диагностика")
    print("2 — Создать резервную копию cups.env")
    print("3 — Показать резервные копии")
    choice = input("Выбор: ").strip()
    if choice == "1":
        diagnostics()
    elif choice == "2":
        backup_management_choice("1")
    elif choice == "3":
        backup_management_choice("2")


def backup_management_choice(choice):
    if choice == "1":
        print("Бэкап:", backup_env())
    elif choice == "2":
        if not BACKUPS.exists():
            print("Бэкапов пока нет")
            return
        for path in sorted(BACKUPS.iterdir(), reverse=True)[:15]:
            if path.is_file():
                print("  " + path.name)
        print("Восстановление бэкапа вручную: старые комнаты могут быть уже удалены")


def main():
    if os.geteuid() != 0:
        sys.exit("Запусти от root: sudo openflux-menu")
    if not Path("/run/systemd/system").exists():
        sys.exit("Не обнаружен запущенный systemd")
    options = {
        "1": install_core_and_service,
        "2": new_rooms,
        "3": show_rooms,
        "4": sync_rooms,
        "5": restart_and_sync,
        "6": show_status,
        "7": print_logs,
        "8": show_key,
        "9": service_control,
        "10": backups_and_diagnostics,
    }
    while True:
        header()
        print(" 1. Установить / обновить OpenFlux + настроить systemd")
        print(" 2. Создать НОВЫЕ комнаты Cups.online (запуск ноды)")
        print(" 3. Показать комнаты и Base64 для iPhone")
        print(" 4. Сохранить комнаты из журнала (после 404)")
        print(" 5. Перезапуск и автоматическая синхронизация")
        print(" 6. Состояние ноды")
        print(" 7. Журнал OpenFlux")
        print(" 8. Показать AES-ключ (с подтверждением)")
        print(" 9. Запуск / остановка / автозагрузка")
        print("10. Диагностика / резервные копии")
        print(" 0. Выход")
        choice = input("\nВыбор: ").strip()
        if choice == "0":
            break
        try:
            action = options.get(choice)
            if action:
                action()
            else:
                print("Неизвестный пункт")
        except MenuError as e:
            print("\nОШИБКА:", e)
        except (KeyboardInterrupt, EOFError):
            print("\nОперация отменена")
        pause()


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print("\nВыход")
