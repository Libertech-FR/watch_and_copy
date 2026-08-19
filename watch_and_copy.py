#!/usr/bin/env python3
"""
Surveille un répertoire source (récursivement) et copie tout nouveau fichier
créé dans un répertoire destination (à plat, sans préserver la structure).

Les répertoires source/destination peuvent être fournis en ligne de commande,
sinon ils sont lus dans config.json (à côté de ce script).

Usage:
    python watch_and_copy.py run     [<source_dir> <dest_dir>]   # foreground
    python watch_and_copy.py start   [<source_dir> <dest_dir>]   # daemon
    python watch_and_copy.py stop
    python watch_and_copy.py status
    python watch_and_copy.py restart [<source_dir> <dest_dir>]   # daemon
"""

import os
import sys
import json
import shutil
import signal
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

PID_FILE = Path("/tmp/watch_and_copy.pid")
LOG_FILE = Path("/tmp/watch_and_copy.log")
CONFIG_FILE = Path("/etc/watch_and_copy/config.json")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def setup_logging(foreground: bool = False):
    logging.basicConfig(
        filename=None if foreground else LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class CopyOnCreate(FileSystemEventHandler):
    def __init__(self, dest: Path):
        self.dest = dest

    def on_created(self, event):
        if event.is_directory:
            return
        src = Path(event.src_path if isinstance(event.src_path, str) else event.src_path.decode())
        dst = self.dest / src.name
        shutil.copy2(src, dst)
        logging.info(f"Copié  {src}  →  {dst}")


def daemonize():
    # Double fork pour détacher du terminal
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)

    # Redirige stdin/stdout/stderr vers /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)

    PID_FILE.write_text(str(os.getpid()))


def get_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def do_run(source: Path, dest: Path):
    """Lance la surveillance en foreground (Ctrl+C pour arrêter)."""
    if not source.is_dir():
        print(f"Erreur : répertoire source introuvable : {source}")
        sys.exit(1)

    dest.mkdir(parents=True, exist_ok=True)
    setup_logging(foreground=True)

    logging.info(f"Surveillance de : {source}")
    logging.info(f"Destination     : {dest}")
    logging.info("Ctrl+C pour arrêter.")

    observer = Observer()
    observer.schedule(CopyOnCreate(dest), str(source), recursive=True)
    observer.start()

    try:
        observer.join()
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
        logging.info("Arrêté.")


def do_start(source: Path, dest: Path):
    if get_pid() is not None:
        print("Le daemon tourne déjà. Utilisez 'restart' pour le relancer.")
        sys.exit(1)

    if not source.is_dir():
        print(f"Erreur : répertoire source introuvable : {source}")
        sys.exit(1)

    dest.mkdir(parents=True, exist_ok=True)

    print(f"Démarrage du daemon…")
    print(f"  Source      : {source}")
    print(f"  Destination : {dest}")
    print(f"  Logs        : {LOG_FILE}")
    print(f"  PID         : {PID_FILE}")

    daemonize()
    setup_logging()

    logging.info(f"Daemon démarré (PID {os.getpid()})")
    logging.info(f"Surveillance de : {source}")
    logging.info(f"Destination     : {dest}")

    observer = Observer()
    observer.schedule(CopyOnCreate(dest), str(source), recursive=True)
    observer.start()

    def on_signal(signum, frame):
        observer.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    observer.join()
    PID_FILE.unlink(missing_ok=True)
    logging.info("Daemon arrêté.")


def do_stop():
    pid = get_pid()
    if pid is None:
        print("Le daemon n'est pas en cours d'exécution.")
        sys.exit(1)
    try:
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        print(f"Daemon arrêté (PID {pid}).")
    except ProcessLookupError:
        print(f"Processus {pid} introuvable — nettoyage du fichier PID.")
        PID_FILE.unlink(missing_ok=True)


def do_status():
    pid = get_pid()
    if pid is None:
        print("Daemon : arrêté")
        return
    try:
        os.kill(pid, 0)
        print(f"Daemon : en cours d'exécution (PID {pid})")
        print(f"Logs   : {LOG_FILE}")
    except ProcessLookupError:
        print(f"Daemon : arrêté (fichier PID périmé)")
        PID_FILE.unlink(missing_ok=True)


USAGE = (
    f"Usage: {sys.argv[0]} run     [<source> <dest>]   # foreground\n"
    f"       {sys.argv[0]} start   [<source> <dest>]   # daemon\n"
    f"       {sys.argv[0]} stop\n"
    f"       {sys.argv[0]} status\n"
    f"       {sys.argv[0]} restart [<source> <dest>]   # daemon\n"
    f"\n"
    f"Si <source>/<dest> sont omis, ils sont lus dans {CONFIG_FILE}."
)


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "stop":
        do_stop()
    elif cmd == "status":
        do_status()
    elif cmd in ("run", "start", "restart"):
        if len(sys.argv) == 4:
            source_raw, dest_raw = sys.argv[2], sys.argv[3]
        elif len(sys.argv) == 2:
            config = load_config()
            source_raw, dest_raw = config.get("source_dir"), config.get("dest_dir")
            if not source_raw or not dest_raw:
                print(f"Erreur : source_dir/dest_dir manquants dans {CONFIG_FILE}")
                sys.exit(1)
        else:
            print(USAGE)
            sys.exit(1)

        source = Path(source_raw).resolve()
        dest = Path(dest_raw).resolve()
        if cmd == "run":
            do_run(source, dest)
        else:
            if cmd == "restart":
                do_stop()
            do_start(source, dest)
    else:
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()