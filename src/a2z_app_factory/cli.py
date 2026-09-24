"""Package, install, verify and run an A2Z agent app."""

import argparse
import json
from pathlib import Path

from .core import install, pack, run, verify


def main() -> None:
    parser = argparse.ArgumentParser(description="A2Z Agent App Factory local bundle tool")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("pack")
    p.add_argument("manifest", type=Path)
    p.add_argument("source_dir", type=Path)
    p.add_argument("--output", required=True, type=Path)
    i = commands.add_parser("install")
    i.add_argument("bundle", type=Path)
    i.add_argument("target", type=Path)
    v = commands.add_parser("verify")
    v.add_argument("target", type=Path)
    r = commands.add_parser("run")
    r.add_argument("target", type=Path)
    r.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "pack":
        result = pack(args.manifest, args.source_dir, args.output)
    elif args.command == "install":
        result = install(args.bundle, args.target)
    elif args.command == "verify":
        result = verify(args.target)
    else:
        trailing = args.args[1:] if args.args[:1] == ["--"] else args.args
        raise SystemExit(run(args.target, trailing))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
