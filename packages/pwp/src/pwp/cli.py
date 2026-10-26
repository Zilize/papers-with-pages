"""Command-line adapters; explicit file persistence lives here, not in the core."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import nullcontext
from datetime import date
from pathlib import Path

from pwp.capture import render_page
from pwp.discover import discover_resources
from pwp.errors import PwpError
from pwp.extract import extract_metadata_links
from pwp.harvest import ArxivClient
from pwp.models import HarvestBatch, Paper, PaperResult
from pwp.page import inspect_page
from pwp.resolve import resolve
from pwp.serialization import from_jsonl


def _parser():
    parser = argparse.ArgumentParser(prog="pwp", description="Metadata-first paper resource tools")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["get", "discover", "resolve"]:
        command = commands.add_parser(name)
        command.add_argument("reference")
        command.add_argument("-o", "--output", type=Path)
        command.add_argument("--progress", action="store_true")
        if name in {"discover", "resolve"}:
            command.add_argument(
                "--github-token",
                default=os.environ.get("PWP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"),
                help="GitHub API token (default: $PWP_GITHUB_TOKEN or $GITHUB_TOKEN)",
            )
        if name == "resolve":
            command.add_argument("--work-dir", type=Path)
            command.add_argument("--no-render", action="store_true")
            command.add_argument("--download-images", action="store_true")
    harvest = commands.add_parser("harvest")
    harvest.add_argument("--set", dest="set_spec")
    harvest.add_argument("--from", dest="date_from", type=date.fromisoformat)
    harvest.add_argument("--until", dest="date_until", type=date.fromisoformat)
    harvest.add_argument("--token")
    harvest.add_argument("--max-batches", type=int)
    harvest.add_argument("--progress", action="store_true")
    harvest.add_argument("-o", "--output", type=Path)
    extract = commands.add_parser("extract")
    extract.add_argument("input", type=Path, help="Paper JSONL, using '-' for stdin")
    extract.add_argument("--input-model", choices=["paper", "batch"], default="paper")
    extract.add_argument("-o", "--output", type=Path)
    page = commands.add_parser("inspect-page")
    page.add_argument("url")
    page.add_argument("-o", "--output", type=Path)
    capture = commands.add_parser("capture")
    capture.add_argument("url")
    capture.add_argument("--work-dir", required=True, type=Path)
    capture.add_argument("-o", "--output", type=Path)
    export = commands.add_parser("export")
    export.add_argument("input", type=Path)
    export.add_argument("--model", choices=["paper", "result"], default="result")
    export.add_argument("--format", choices=["json", "jsonl"], default="jsonl")
    export.add_argument("-o", "--output", type=Path)
    return parser


def _progress(event):
    print(event.model_dump_json(), file=sys.stderr)


def _write(stream, value):
    if hasattr(value, "model_dump_json"):
        stream.write(value.model_dump_json() + "\n")
    else:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    stream.flush()


async def _run(args, stream):
    callback = _progress if getattr(args, "progress", False) else None
    if args.command in {"discover", "resolve"} and not args.github_token:
        print(
            "pwp: no GitHub token; the API allows 60 unauthenticated requests per hour. "
            "Set PWP_GITHUB_TOKEN or pass --github-token.",
            file=sys.stderr,
        )
    if args.command in {"get", "discover", "resolve", "harvest"}:
        async with ArxivClient() as client:
            if args.command == "get":
                _write(stream, await client.get_paper(args.reference, on_event=callback))
            elif args.command == "discover":
                paper = await client.get_paper(args.reference, on_event=callback)
                _write(
                    stream,
                    await discover_resources(
                        paper, github_token=args.github_token, on_event=callback
                    ),
                )
            elif args.command == "resolve":
                result = await resolve(
                    args.reference,
                    work_dir=args.work_dir,
                    render=not args.no_render,
                    download_images=args.download_images,
                    metadata_client=client,
                    github_token=args.github_token,
                    on_event=callback,
                )
                _write(stream, result)
                return 1 if result.status == "failed" else 0
            else:
                if args.max_batches is not None and args.max_batches < 1:
                    raise ValueError("max-batches must be positive")
                iterator = client.harvest(
                    set_spec=args.set_spec,
                    metadata_updated_from=args.date_from,
                    metadata_updated_until=args.date_until,
                    resumption_token=args.token,
                    on_event=callback,
                )
                try:
                    count = 0
                    async for batch in iterator:
                        _write(stream, batch)
                        count += 1
                        if args.max_batches and count >= args.max_batches:
                            break
                finally:
                    await iterator.aclose()
    elif args.command == "inspect-page":
        _write(stream, await inspect_page(args.url))
    elif args.command == "capture":
        result = await render_page(args.url, work_dir=args.work_dir)
        _write(
            stream,
            {
                "page": result.page.model_dump(mode="json"),
                "screenshot": result.screenshot.model_dump(mode="json")
                if result.screenshot
                else None,
                "issues": [issue.model_dump() for issue in result.issues],
            },
        )
    elif args.command in {"extract", "export"}:
        source = (
            nullcontext(sys.stdin) if str(args.input) == "-" else args.input.open(encoding="utf-8")
        )
        with source as lines:
            if args.command == "extract":
                model = Paper if args.input_model == "paper" else HarvestBatch
            else:
                model = Paper if args.model == "paper" else PaperResult
            records = from_jsonl(lines, model=model)
            if args.command == "export" and args.format == "json":
                _write(stream, [record.model_dump(mode="json") for record in records])
            else:
                for record in records:
                    if args.command == "extract":
                        if isinstance(record, HarvestBatch):
                            for item in record.records:
                                _write(
                                    stream,
                                    {
                                        "record": item.model_dump(mode="json"),
                                        "links": [
                                            link.model_dump(mode="json")
                                            for link in extract_metadata_links(item.paper)
                                        ]
                                        if item.paper
                                        else [],
                                    },
                                )
                            continue
                        _write(
                            stream,
                            {
                                "paper": record.model_dump(mode="json"),
                                "links": [
                                    link.model_dump(mode="json")
                                    for link in extract_metadata_links(record)
                                ],
                            },
                        )
                    else:
                        _write(stream, record)
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "input", None) and args.output and str(args.input) != "-":
            if args.input.resolve() == args.output.resolve():
                raise ValueError("Input and output paths must differ")
        target = args.output.open("w", encoding="utf-8") if args.output else nullcontext(sys.stdout)
        with target as stream:
            return asyncio.run(_run(args, stream))
    except (PwpError, ValueError, OSError) as exc:
        print(f"pwp: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
