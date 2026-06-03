import argparse
import json
from datetime import date, datetime
from pathlib import Path

from .classifier import classify_image_detail
from .callback_server import serve_wecom_callback
from .contact_roles import (
    auto_bind_contacts_from_unknown_archive,
    contacts_by_role,
    contacts_to_suppliers,
    load_contact_roles,
    merge_contact_roles,
    serve_role_manager,
    write_contact_roles,
)
from .collector import ingest_supplier_images
from .config import config
from .desktop_plan import build_daily_question_plan, write_desktop_plan
from .desktop_outbox import load_outbox, mark_outbox_sent, pending_outbox_tasks
from .desktop_receiver import (
    check_desktop_capture,
    capture_selector_selections,
    capture_waiting_supplier_images,
    open_screen_capture_settings,
)
from .desktop_sender import check_desktop_automation, open_accessibility_settings, send_pending_desktop_outbox
from .douyin_listing import (
    build_listing_drafts,
    build_listing_drafts_from_csv,
    choose_products,
    load_listing_defaults,
    load_selection_product_ids,
    mark_drafted_products,
    write_listing_outputs,
)
from .demo import demo_received_at, make_demo_images, seed_demo
from .health import health_payload, run_health_checks
from .inbox_events import InboxEvent, process_pending_inbox_events, queue_inbox_event
from .images import list_images
from .report import build_daily_report
from .scheduler import build_batches, should_ask_supplier
from .selection import detect_selections, make_demo_selection, write_selection_json
from .storage import Store
from .task_state import load_tasks, mark_tasks_sent, pending_reply_tasks, write_tasks
from .wecom import WeComClient
from .wecom_archive import poll_message_archive_into_inbox
from .workflow_state import (
    advance_supplier_status,
    initialize_daily_workflow,
    load_daily_workflow,
    workflow_summary,
    write_daily_workflow,
)
from .workflow_engine import WorkflowEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="企业微信供应商新款采集机器人 V1")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("seed-demo")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--skip-live", action="store_true", help="只检查本地配置，不请求企业微信官方接口")

    ask = sub.add_parser("ask")
    ask.add_argument("--date", default=date.today().isoformat())
    ask.add_argument("--batch-size", type=int, default=10)
    ask.add_argument("--batch-index", type=int, default=0)

    demo_images = sub.add_parser("import-demo-images")
    demo_images.add_argument("--date", default=date.today().isoformat())

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--supplier-id", required=True)
    ingest.add_argument("--date", default=date.today().isoformat())
    ingest.add_argument("paths", nargs="+")

    queue_event = sub.add_parser("queue-inbox-event")
    queue_event.add_argument("--supplier-id", required=True)
    queue_event.add_argument("--received-at")
    queue_event.add_argument("--source", default="manual")
    queue_event.add_argument("--text", default="")
    queue_event.add_argument("--image", action="append", default=[])
    queue_event.add_argument("--event-id")

    sub.add_parser("process-inbox-events")

    archive_poll = sub.add_parser("poll-wecom-archive")
    archive_poll.add_argument("--date", default=date.today().isoformat())
    archive_poll.add_argument("--limit", type=int)

    report = sub.add_parser("build-report")
    report.add_argument("--date", default=date.today().isoformat())
    report.add_argument("--use-ai-style", action="store_true", help="用视觉模型按款式分组，只在报表中折叠同款多图")

    reclassify = sub.add_parser("reclassify")
    reclassify.add_argument("--date", default=date.today().isoformat())
    reclassify.add_argument("--product-id")

    demo_select = sub.add_parser("make-demo-selection")
    demo_select.add_argument("--date", default=date.today().isoformat())

    detect = sub.add_parser("detect-selection")
    detect.add_argument("--date", default=date.today().isoformat())
    detect.add_argument("--screenshot", required=True)

    samples = sub.add_parser("request-samples")
    samples.add_argument("--date", default=date.today().isoformat())
    samples.add_argument("--selection", required=True)

    test_wecom = sub.add_parser("test-wecom")
    test_wecom.add_argument("--text", default="企业微信供应商机器人测试消息")

    sync_contacts = sub.add_parser("sync-wecom-contacts")
    sync_contacts.add_argument("--owner-userid", action="append", help="客户归属成员 userid；不填则读取已配置客户联系成员")
    sync_contacts.add_argument("--output", default="data/wecom_contacts.json")

    bind_unknown = sub.add_parser("bind-unknown-contacts")
    bind_unknown.add_argument("--contacts", default="data/wecom_contacts.json")

    contact_roles = sub.add_parser("manage-contact-roles")
    contact_roles.add_argument("--contacts", default="data/wecom_contacts.json")
    contact_roles.add_argument("--host", default="127.0.0.1")
    contact_roles.add_argument("--port", type=int, default=8765)

    init_workflow = sub.add_parser("init-daily-workflow")
    init_workflow.add_argument("--date", default=date.today().isoformat())
    init_workflow.add_argument("--contacts", default="data/wecom_contacts.json")
    init_workflow.add_argument("--output")

    show_workflow = sub.add_parser("show-daily-workflow")
    show_workflow.add_argument("--date", default=date.today().isoformat())
    show_workflow.add_argument("--path")

    mark_workflow = sub.add_parser("mark-workflow-supplier")
    mark_workflow.add_argument("--date", default=date.today().isoformat())
    mark_workflow.add_argument("--supplier-id", required=True)
    mark_workflow.add_argument("--status", required=True)
    mark_workflow.add_argument("--path")

    run_workflow = sub.add_parser("run-workflow-once")
    run_workflow.add_argument("--date", default=date.today().isoformat())
    run_workflow.add_argument("--no-ai-style", action="store_true")
    run_workflow.add_argument("--send-internal", action="store_true")

    callback_server = sub.add_parser("serve-wecom-callback")
    callback_server.add_argument("--host", default="127.0.0.1")
    callback_server.add_argument("--port", type=int, default=8787)

    show_outbox = sub.add_parser("show-desktop-outbox")
    show_outbox.add_argument("--date", default=date.today().isoformat())
    show_outbox.add_argument("--pending-only", action="store_true")
    show_outbox.add_argument("--next", action="store_true")

    mark_outbox = sub.add_parser("mark-outbox-sent")
    mark_outbox.add_argument("--date", default=date.today().isoformat())
    mark_outbox.add_argument("task_ids", nargs="+")

    send_outbox = sub.add_parser("send-desktop-outbox")
    send_outbox.add_argument("--date", default=date.today().isoformat())
    send_outbox.add_argument("--limit", type=int, default=1)
    send_outbox.add_argument("--kind", action="append")
    send_outbox.add_argument("--dry-run", action="store_true")

    sub.add_parser("check-desktop-automation")
    sub.add_parser("check-desktop-capture")
    capture_desktop = sub.add_parser("capture-desktop-replies")
    capture_desktop.add_argument("--date", default=date.today().isoformat())
    sub.add_parser("open-accessibility-settings")
    sub.add_parser("open-screen-capture-settings")

    desktop_plan = sub.add_parser("build-desktop-ask-plan")
    desktop_plan.add_argument("--date", default=date.today().isoformat())
    desktop_plan.add_argument("--batch-size", type=int, default=10)
    desktop_plan.add_argument("--batch-index", type=int, default=0)
    desktop_plan.add_argument("--supplier-id", action="append")
    desktop_plan.add_argument("--output")

    mark_sent = sub.add_parser("mark-desktop-sent")
    mark_sent.add_argument("--plan", required=True)

    pending_replies = sub.add_parser("pending-replies")
    pending_replies.add_argument("--plan", required=True)

    douyin_drafts = sub.add_parser("build-douyin-drafts")
    douyin_drafts.add_argument("--date")
    douyin_drafts.add_argument("--selection")
    douyin_drafts.add_argument("--product-id", action="append")
    douyin_drafts.add_argument("--defaults")
    douyin_drafts.add_argument("--output-dir")
    douyin_drafts.add_argument("--mark-drafted", action="store_true")

    douyin_csv_drafts = sub.add_parser("build-douyin-drafts-from-csv")
    douyin_csv_drafts.add_argument("--csv", required=True)
    douyin_csv_drafts.add_argument("--image-dir")
    douyin_csv_drafts.add_argument("--defaults")
    douyin_csv_drafts.add_argument("--output-dir")
    douyin_csv_drafts.add_argument("--product-name")
    douyin_csv_drafts.add_argument("--external-code")

    args = parser.parse_args()
    store = Store(config.db_path)

    if args.command == "init":
        store.init()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        print(f"Initialized database: {config.db_path}")
        return

    store.init()

    if args.command == "doctor":
        print(json.dumps(health_payload(run_health_checks(config, live_api=not args.skip_live)), ensure_ascii=False, indent=2))
    elif args.command == "seed-demo":
        seed_demo(store, config.data_dir / "suppliers.json")
        print("Seeded demo suppliers")
    elif args.command == "ask":
        client = WeComClient(config)
        run_date = date.fromisoformat(args.date)
        suppliers = [supplier for supplier in store.list_suppliers() if should_ask_supplier(supplier, run_date)]
        batches = build_batches(suppliers, args.batch_size)
        batch = batches[args.batch_index] if 0 <= args.batch_index < len(batches) else []
        client.send_daily_question(batch, args.date)
        print(f"Asked {len(batch)} suppliers in batch {args.batch_index + 1}/{max(len(batches), 1)}")
    elif args.command == "import-demo-images":
        demo_dir = config.data_dir / "samples" / args.date
        make_demo_images(demo_dir)
        mapping = {
            "S01": [demo_dir / "tee_white.jpg", demo_dir / "shirt_blue.jpg"],
            "S02": [demo_dir / "dress_red.jpg", demo_dir / "skirt_black.jpg"],
            "S03": [demo_dir / "coat_green.jpg", demo_dir / "pants_gray.jpg"],
        }
        for supplier_id, paths in mapping.items():
            created = ingest_supplier_images(store, config.data_dir, supplier_id, paths, demo_received_at(args.date))
            print(f"{supplier_id}: imported {len(created)} products")
    elif args.command == "ingest":
        paths = list_images([Path(item) for item in args.paths])
        received_at = datetime.fromisoformat(f"{args.date}T{datetime.now().strftime('%H:%M:%S')}")
        created = ingest_supplier_images(store, config.data_dir, args.supplier_id, paths, received_at)
        print(f"Imported {len(created)} products for {args.supplier_id}")
    elif args.command == "queue-inbox-event":
        received_at = args.received_at or datetime.now().isoformat(timespec="seconds")
        event_id = args.event_id or f"{args.supplier_id}-{datetime.fromisoformat(received_at).strftime('%Y%m%d%H%M%S')}"
        path = queue_inbox_event(
            config.data_dir,
            InboxEvent(
                event_id=event_id,
                supplier_id=args.supplier_id,
                received_at=received_at,
                image_paths=args.image,
                text=args.text,
                source=args.source,
            ),
        )
        print(f"Inbox event: {path}")
    elif args.command == "process-inbox-events":
        result = process_pending_inbox_events(store, config.data_dir)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == "poll-wecom-archive":
        result = poll_message_archive_into_inbox(
            config,
            store,
            config.data_dir,
            args.date,
            limit=args.limit,
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == "build-report":
        report_dir = config.data_dir / "reports" / args.date
        png, pdf, manifest = build_daily_report(store, args.date, report_dir, use_ai_style=args.use_ai_style)
        print(f"Report image: {png}")
        print(f"Report PDF: {pdf}")
        print(f"Manifest: {manifest}")
    elif args.command == "reclassify":
        products = [store.get_product(args.product_id)] if args.product_id else store.list_products_for_date(args.date)
        updated = 0
        for product in [item for item in products if item is not None]:
            supplier = store.get_supplier(product.supplier_id)
            supplier_categories = supplier.main_categories if supplier else []
            result = classify_image_detail(product.primary_image, supplier_categories)
            product.category_lv1 = result.category_lv1
            product.category_lv2 = result.category_lv2
            product.confidence = result.confidence
            store.upsert_product(product)
            updated += 1
            review = " needs_review" if result.needs_review else ""
            print(f"{product.product_id}: {result.category_lv1}/{result.category_lv2} {result.confidence:.2f}{review}")
        print(f"Reclassified {updated} products")
    elif args.command == "make-demo-selection":
        report_dir = config.data_dir / "reports" / args.date
        output = make_demo_selection(report_dir / "report.png", report_dir / "manifest.json", report_dir / "demo_selection.png")
        print(f"Demo selection screenshot: {output}")
    elif args.command == "detect-selection":
        report_dir = config.data_dir / "reports" / args.date
        selections = detect_selections(report_dir / "manifest.json", Path(args.screenshot))
        output = write_selection_json(selections, report_dir / "selection.json")
        print(json.dumps([selection.__dict__ for selection in selections], ensure_ascii=False, indent=2))
        print(f"Selection JSON: {output}")
    elif args.command == "request-samples":
        payload = json.loads(Path(args.selection).read_text(encoding="utf-8"))
        product_ids = [item["product_id"] for item in payload]
        WeComClient(config).request_samples(store, product_ids)
        print(f"Requested samples for {len(product_ids)} products")
    elif args.command == "test-wecom":
        WeComClient(config).send_test_message(args.text)
    elif args.command == "sync-wecom-contacts":
        output = Path(args.output)
        existing = load_contact_roles(output)
        incoming = WeComClient(config).sync_external_contacts(args.owner_userid)
        contacts = merge_contact_roles(existing, incoming)
        write_contact_roles(output, contacts)
        print(f"WeCom contacts: {output}")
        print(f"Contacts: {len(contacts)}")
    elif args.command == "bind-unknown-contacts":
        changed = auto_bind_contacts_from_unknown_archive(Path(args.contacts), config.data_dir)
        for contact in changed:
            for supplier in contacts_to_suppliers([contact]):
                store.upsert_supplier(supplier)
            print(f"{contact.display_name}: {contact.external_user_id}")
        print(f"Bound contacts: {len(changed)}")
    elif args.command == "manage-contact-roles":
        serve_role_manager(Path(args.contacts), host=args.host, port=args.port)
    elif args.command == "init-daily-workflow":
        contacts = load_contact_roles(Path(args.contacts))
        role_suppliers = contacts_to_suppliers(contacts)
        run_date = datetime.fromisoformat(args.date).date()
        suppliers = [supplier for supplier in (role_suppliers or store.list_suppliers()) if should_ask_supplier(supplier, run_date)]
        workflow_path = Path(args.output) if args.output else config.data_dir / "tasks" / args.date / "daily_workflow.json"
        workflow = initialize_daily_workflow(
            args.date,
            suppliers,
            contacts_by_role(contacts, "selector"),
            contacts_by_role(contacts, "operator"),
            existing=load_daily_workflow(workflow_path),
        )
        write_daily_workflow(workflow_path, workflow)
        print(f"Daily workflow: {workflow_path}")
        print(json.dumps(workflow_summary(workflow), ensure_ascii=False, indent=2))
    elif args.command == "show-daily-workflow":
        workflow_path = Path(args.path) if args.path else config.data_dir / "tasks" / args.date / "daily_workflow.json"
        workflow = load_daily_workflow(workflow_path)
        if not workflow:
            print(f"No workflow found: {workflow_path}")
            return
        print(json.dumps(workflow.__dict__ | {"summary": workflow_summary(workflow)}, ensure_ascii=False, indent=2, default=lambda obj: obj.__dict__))
    elif args.command == "mark-workflow-supplier":
        workflow_path = Path(args.path) if args.path else config.data_dir / "tasks" / args.date / "daily_workflow.json"
        workflow = load_daily_workflow(workflow_path)
        if not workflow:
            raise SystemExit(f"No workflow found: {workflow_path}")
        advance_supplier_status(workflow, args.supplier_id, args.status)
        write_daily_workflow(workflow_path, workflow)
        print(f"Updated {args.supplier_id} -> {args.status}")
    elif args.command == "run-workflow-once":
        result = WorkflowEngine(config, store).run_once(
            date.fromisoformat(args.date),
            use_ai_style=not args.no_ai_style,
            send_internal=args.send_internal,
        )
        print(f"Workflow: {result.workflow_path}")
        print(json.dumps({"actions": result.actions, "summary": result.summary}, ensure_ascii=False, indent=2))
    elif args.command == "serve-wecom-callback":
        serve_wecom_callback(config, host=args.host, port=args.port)
    elif args.command == "show-desktop-outbox":
        path = config.data_dir / "tasks" / args.date / "desktop_outbox.json"
        tasks = load_outbox(path)
        if args.pending_only or args.next:
            tasks = pending_outbox_tasks(tasks)
        if args.next:
            tasks = tasks[:1]
        print(json.dumps([task.__dict__ for task in tasks], ensure_ascii=False, indent=2))
        print(f"Outbox: {path}")
    elif args.command == "mark-outbox-sent":
        path = config.data_dir / "tasks" / args.date / "desktop_outbox.json"
        tasks = mark_outbox_sent(path, args.task_ids)
        print(f"Marked sent: {', '.join(args.task_ids)}")
        print(f"Pending: {len(pending_outbox_tasks(tasks))}")
    elif args.command == "send-desktop-outbox":
        path = config.data_dir / "tasks" / args.date / "desktop_outbox.json"
        result = send_pending_desktop_outbox(path, limit=args.limit, kinds=args.kind, dry_run=args.dry_run)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == "check-desktop-automation":
        print(json.dumps(check_desktop_automation().__dict__, ensure_ascii=False, indent=2))
    elif args.command == "check-desktop-capture":
        print(json.dumps(check_desktop_capture().__dict__, ensure_ascii=False, indent=2))
    elif args.command == "capture-desktop-replies":
        run_date = date.fromisoformat(args.date)
        results = {
            "supplier_images": capture_waiting_supplier_images(config.data_dir, run_date),
            "selector_selections": capture_selector_selections(config.data_dir, run_date),
        }
        print(
            json.dumps(
                {
                    key: value.__dict__ | {"captures": [capture.__dict__ for capture in value.captures]}
                    for key, value in results.items()
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "open-accessibility-settings":
        open_accessibility_settings()
        print("已打开 macOS 辅助功能设置。请允许“终端”控制电脑，然后重新运行 check-desktop-automation。")
    elif args.command == "open-screen-capture-settings":
        open_screen_capture_settings()
        print("已打开 macOS 屏幕录制设置。请允许“终端”截屏，然后重新运行 check-desktop-capture。")
    elif args.command == "build-desktop-ask-plan":
        tasks = build_daily_question_plan(store, args.date, args.batch_size, args.batch_index, args.supplier_id)
        output = Path(args.output) if args.output else config.data_dir / "tasks" / args.date / f"desktop_ask_batch_{args.batch_index + 1}.json"
        write_desktop_plan(tasks, output)
        print(f"Desktop ask plan: {output}")
        print(f"Tasks: {len(tasks)}")
    elif args.command == "mark-desktop-sent":
        plan_path = Path(args.plan)
        tasks = mark_tasks_sent(load_tasks(plan_path))
        write_tasks(tasks, plan_path)
        print(f"Marked waiting_reply: {len(tasks)}")
    elif args.command == "pending-replies":
        tasks = pending_reply_tasks(load_tasks(Path(args.plan)))
        print(json.dumps([task.__dict__ for task in tasks], ensure_ascii=False, indent=2))
    elif args.command == "build-douyin-drafts":
        product_ids = []
        if args.selection:
            product_ids.extend(load_selection_product_ids(Path(args.selection)))
        if args.product_id:
            product_ids.extend(args.product_id)
        defaults = load_listing_defaults(Path(args.defaults) if args.defaults else None)
        products = choose_products(store, args.date, product_ids)
        drafts = build_listing_drafts(store, products, defaults)
        output_date = args.date or (Path(args.selection).parent.name if args.selection else datetime.now().strftime("%Y-%m-%d"))
        base_output_dir = (
            Path(args.output_dir)
            if args.output_dir
            else config.data_dir / "listings" / output_date
        )
        json_path, csv_path = write_listing_outputs(drafts, base_output_dir)
        if args.mark_drafted:
            mark_drafted_products(store, drafts)
        ready_count = sum(1 for draft in drafts if draft.ready_to_publish)
        print(f"Douyin listing drafts: {json_path}")
        print(f"CSV review sheet: {csv_path}")
        print(f"Drafts: {len(drafts)} ready_to_publish: {ready_count} needs_review: {len(drafts) - ready_count}")
    elif args.command == "build-douyin-drafts-from-csv":
        defaults = load_listing_defaults(Path(args.defaults) if args.defaults else None)
        image_dir = Path(args.image_dir) if args.image_dir else None
        drafts = build_listing_drafts_from_csv(
            Path(args.csv),
            image_dir,
            defaults,
            product_name=args.product_name or "",
            external_code=args.external_code or "",
        )
        output_dir = Path(args.output_dir) if args.output_dir else config.data_dir / "listings" / Path(args.csv).stem
        json_path, csv_path = write_listing_outputs(drafts, output_dir)
        ready_count = sum(1 for draft in drafts if draft.ready_to_publish)
        print(f"Douyin listing drafts: {json_path}")
        print(f"CSV review sheet: {csv_path}")
        print(f"Drafts: {len(drafts)} ready_to_publish: {ready_count} needs_review: {len(drafts) - ready_count}")


if __name__ == "__main__":
    main()
