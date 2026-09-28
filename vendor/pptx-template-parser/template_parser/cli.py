import argparse
import json
from pathlib import Path

from PIL import Image

from .extract import extract_template, write_json, sha256
from .vlm import analyze_slide, PROMPT_VERSION, ModelOutputError
from .review import create_review
from .render import render_package
from .api import APIError
from .composition import compose_slide
from .batch import run_batch
from .exporter import export_package
from datetime import datetime
from .contracts import check_package


def main():
    parser = argparse.ArgumentParser(description="Extract and semantically decompose PPTX templates")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser('serve', help='Local read-only result API with background PPTX processing')
    server.add_argument('--config', type=Path, required=True)
    server.add_argument('--storage', type=Path, default=Path('output/api_jobs'))
    server.add_argument('--port', type=int, default=8765)
    server.add_argument('--origin', help='Exact frontend origin, e.g. http://localhost:5173')
    check = sub.add_parser('check', help='Validate package contracts, references and file hashes offline')
    check.add_argument('package', type=Path)
    export = sub.add_parser('export', help='Export all structurally valid decompositions without API')
    export.add_argument('package', type=Path)
    export.add_argument('--output', type=Path, required=True)
    run = sub.add_parser('run', help='Extract/render/analyze/export; resume an existing package')
    run.add_argument('input', type=Path, help='PPTX file or existing extracted package')
    run.add_argument('--config', type=Path, required=True)
    run.add_argument('--package', type=Path, help='Working package directory for PPTX input')
    run.add_argument('--output', type=Path, help='New export directory; defaults to timestamped sibling')
    run.add_argument('--delay', type=int, default=10)
    extract = sub.add_parser("extract")
    extract.add_argument("source", type=Path)
    extract.add_argument("--output", type=Path, required=True)
    render = sub.add_parser("render", help="Render an extracted package using desktop PowerPoint")
    render.add_argument("package", type=Path)
    render.add_argument("--width", type=int, default=1600)
    render.add_argument("--timeout", type=int, default=300)
    render.add_argument('--renderer', choices=['auto','powerpoint','libreoffice'], default='auto')
    run.add_argument('--renderer', choices=['auto','powerpoint','libreoffice'], default='auto')
    analyze = sub.add_parser("analyze")
    analyze.add_argument("package", type=Path)
    analyze.add_argument("--config", type=Path, required=True)
    selection = analyze.add_mutually_exclusive_group(required=True)
    selection.add_argument("--slide", type=int, help="One-based slide index")
    selection.add_argument("--all", action="store_true", help="Analyze all slides; resume current valid results")
    analyze.add_argument("--delay", type=int, default=10, help="Seconds between slide requests in batch mode (0-60)")
    analyze.add_argument("--no-retry", action="store_true", help="Make one API attempt and show diagnostics on failure")
    review = sub.add_parser("review", help="Create an offline HTML overlay for existing semantics")
    review.add_argument("package", type=Path)
    review.add_argument("--slide", type=int, required=True)
    args = parser.parse_args()
    if args.command == 'serve':
        import os
        from .server import serve
        token = os.environ.get('PARSER_API_TOKEN', '')
        try:
            serve(args.storage, json.loads(args.config.read_text(encoding='utf-8')), token, args.port, args.origin)
        except (ValueError, OSError) as exc:
            parser.exit(1, f'API startup error: {exc}\n')
        return
    if args.command == 'check':
        try:
            report = check_package(args.package)
        except (ValueError, OSError, KeyError) as exc:
            parser.exit(1, f'Package check failed: {exc}\n')
        print(f"Checked: {report['counts']}")
        if report['counts'].get('invalid', 0):
            parser.exit(1)
        return
    if args.command == 'export':
        try:
            print(json.dumps(export_package(args.package, args.output), ensure_ascii=False))
        except (ValueError, OSError, KeyError) as exc:
            parser.exit(1, f'Export error: {exc}\n')
        return
    if args.command == 'run':
        if not 0 <= args.delay <= 60:
            parser.error('--delay must be between 0 and 60')
        package = args.input if args.input.is_dir() else args.package
        if package is None:
            parser.error('PPTX input requires --package')
        target = args.output or package.parent / (package.name + '_export_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        if target.exists() or target.with_suffix('.zip').exists():
            parser.error('Export destination exists; choose a new --output')
        try:
            config = json.loads(args.config.read_text(encoding='utf-8'))
            if not args.input.is_dir():
                if not package.exists():
                    extract_template(args.input, package)
                elif sha256(args.input.read_bytes()) != json.loads((package/'template.json').read_text(encoding='utf-8'))['source']['sha256']:
                    raise ValueError('Working package belongs to a different source')
            if not (package/'previews').exists():
                render_package(package, 1600, 300, renderer=args.renderer)
            report = run_batch(package, config, delay=args.delay)
            print(json.dumps(export_package(package, target), ensure_ascii=False))
            if report['status'] != 'finished':
                parser.exit(130 if report['status']=='interrupted' else 1,
                            f"Partial export saved; batch status: {report['status']}\n")
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            parser.exit(1, f'Pipeline error: {exc}\n')
        return
    if args.command == "review":
        print(f"Review written to {create_review(args.package, args.slide)}")
        return
    if args.command == "extract":
        manifest = extract_template(args.source, args.output)
        print(f"Extracted {len(manifest['slides'])} slides into {args.output}")
        return
    if args.command == "render":
        report = render_package(args.package, args.width, args.timeout, renderer=args.renderer)
        print(f"Rendered and verified {len(report['slides'])} slides into {args.package / 'previews'}")
        return
    if args.all:
        if not 0 <= args.delay <= 60:
            parser.error('--delay must be between 0 and 60 seconds')
        config = json.loads(args.config.read_text(encoding='utf-8'))
        if args.no_retry:
            config.update(max_retries=0, validation_retries=0)
        try:
            report = run_batch(args.package, config, delay=args.delay)
        except (ValueError, OSError, KeyError) as exc:
            parser.exit(1, f'Batch error: {exc}\n')
        print(f"Batch: {report['status']}; {report['counts']}. Report: {args.package / 'analysis_report.json'}")
        if report['status'] != 'finished':
            parser.exit(130 if report['status'] == 'interrupted' else 1)
        return
    manifest = json.loads((args.package / "template.json").read_text(encoding="utf-8"))
    if not 1 <= args.slide <= len(manifest["slides"]):
        parser.error("Slide index out of range")
    slide = compose_slide(manifest, manifest["slides"][args.slide - 1])
    preview = args.package / "previews" / f"{slide['id']}.png"
    with Image.open(preview) as image:
        image.verify()
        if image.format != "PNG":
            raise ValueError("Preview must be PNG")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.no_retry:
        config["max_retries"] = 0
        config["validation_retries"] = 0
    try:
        result = analyze_slide(slide, preview, config, rejected_dir=args.package / "diagnostics" / slide["id"])
    except APIError as exc:
        parser.exit(1, f"API error: {exc}\n")
    except ModelOutputError as exc:
        parser.exit(1, f"Model output error: {exc}\n")
    out = args.package / "semantics"
    out.mkdir(exist_ok=True)
    write_json(out / f"{slide['id']}.json", {
        "schema_version": "0.2", "source_sha256": manifest["source"]["sha256"],
        "model": config["model"], "prompt_version": PROMPT_VERSION,
        "preview_sha256": sha256(preview.read_bytes()), "result": result.model_dump(),
        "object_scope": "composed_v1",
        "object_catalog": slide["objects"],
    })
    print(f"Validated semantics written to {out / (slide['id'] + '.json')}")


if __name__ == "__main__":
    main()
