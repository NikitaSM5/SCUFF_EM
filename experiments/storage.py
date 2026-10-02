"""Atomic result manifests, cache identities and append-only experiment logs."""
import csv
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def key(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temp.write_bytes(canonical(data)+b"\n")
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_csv(path, rows, *, fields=None):
    rows = list(rows)
    fields = fields if fields is not None else list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=True) if isinstance(v, (dict, list)) else v for k, v in row.items()})


class Journal:
    def __init__(self, directory, emit):
        self.directory = Path(directory)
        self.emit = emit
        self.lock = threading.RLock()

    def event(self, event, **data):
        with self.lock:
            with (self.directory / "events.jsonl").open("ab") as stream:
                stream.write(canonical(dict(event=event, **data))+b"\n")
            if event in ("log", "stage", "error"):
                with (self.directory / "experiment.log").open("a", encoding="utf-8") as stream:
                    stream.write(str(data.get("message", data))+"\n")
            self.emit(event, **data)

    def append(self, name, data):
        with self.lock, (self.directory / name).open("ab") as stream:
            stream.write(canonical(data)+b"\n")


class CalculationCache:
    def __init__(self, directory, namespace):
        self.directory = Path(directory) / namespace
        self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, identity):
        path = self.directory / (key(identity)+".json")
        if not path.is_file():
            return None
        data = read_json(path)
        if data.get("identity") != identity or data.get("payload_sha256") != key(data["payload"]):
            raise ValueError(f"Corrupt calculation cache: {path}")
        for file in data["payload"].get("artifacts", []):
            if not Path(file["path"]).is_file() or file_hash(file["path"]) != file["sha256"]:
                raise ValueError(f"Missing or modified cache artifact: {file['path']}")
        return data["payload"]

    def put(self, identity, payload):
        write_json(self.directory / (key(identity)+".json"),
                   dict(identity=identity, payload=payload, payload_sha256=key(payload)))
