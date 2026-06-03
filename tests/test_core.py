import hashlib
import json
import tempfile
import threading
import unittest
from io import BytesIO
from datetime import datetime, time as day_time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from PIL import Image

from supplier_bot.alerts import send_alert_email, send_receive_channel_failure_alert
from supplier_bot.callback_server import make_wecom_callback_server
from supplier_bot.classifier import classify_image, classify_image_detail
from supplier_bot.config import Config, config, load_env_file
from supplier_bot.contact_roles import (
    ROLE_CONFIRMER,
    ROLE_OPERATOR,
    ROLE_SELECTOR,
    ROLE_SUPPLIER,
    ContactRole,
    auto_bind_contacts_from_unknown_archive,
    contact_from_external_payload,
    contacts_by_role,
    contacts_to_suppliers,
    load_contact_roles,
    merge_contact_roles,
    write_contact_roles,
)
from supplier_bot.collector import ingest_supplier_images
from supplier_bot.desktop_plan import build_daily_question_plan, write_desktop_plan
from supplier_bot.demo import demo_received_at, make_demo_images
from supplier_bot.douyin_listing import (
    ListingDefaults,
    build_listing_drafts,
    build_listing_drafts_from_csv,
    choose_products,
    infer_category,
    load_csv_listing_rows,
    load_selection_product_ids,
    mark_drafted_products,
    match_listing_images,
    parse_stock_skus,
    write_listing_outputs,
)
from supplier_bot.desktop_outbox import load_outbox, mark_outbox_sent, pending_outbox_tasks
from supplier_bot.desktop_receiver import DesktopCapture, capture_selector_selections, extract_incoming_image_crops
from supplier_bot.desktop_sender import send_pending_desktop_outbox
from supplier_bot.health import health_payload, run_health_checks
from supplier_bot.inbox_events import InboxEvent, process_pending_inbox_events, queue_inbox_event
from supplier_bot.images import phash
from supplier_bot.models import Product, ProductStatus, Supplier
from supplier_bot.ops_table import build_ops_table
from supplier_bot.report import build_daily_report
from supplier_bot.receive_recovery import (
    diagnose_receive_channel,
    receive_recovery_required,
    reconcile_receive_recovery,
    record_receive_channel_failure,
    record_receive_channel_recovery,
)
from supplier_bot.sample_requests import build_sample_request_tasks
from supplier_bot.scheduler import build_batches, is_supplier_rest_day, should_ask_supplier
from supplier_bot.selection import detect_selection_text, detect_selections, make_demo_selection
from supplier_bot.storage import Store
from supplier_bot.style_ai import _cards_from_groups, _normalize_groups
from supplier_bot.style_merge import classify_image_role
from supplier_bot.task_state import load_tasks, mark_tasks_sent, pending_reply_tasks
from supplier_bot.wecom import WeComClient
from supplier_bot.wecom_archive import (
    _contact_lookup,
    normalize_archive_message,
    parse_msgtime,
    poll_message_archive_into_inbox,
    recover_bound_unknown_archive_events,
)
from supplier_bot.wecom_crypto import encrypt_callback_payload
from supplier_bot.workflow_engine import WorkflowEngine, _selection_sample_request_readiness
from supplier_bot.workflow_state import (
    STATUS_ASK_SENT,
    STATUS_INFO_RECEIVED,
    STATUS_SELECTION_RECEIVED,
    STATUS_PENDING_ASK,
    STATUS_REPORT_SENT,
    STATUS_SAMPLE_REQUESTED,
    STATUS_WAITING_IMAGES,
    advance_supplier_status,
    initialize_daily_workflow,
    load_daily_workflow,
    suppliers_needing_ask,
    workflow_summary,
    write_daily_workflow,
)
from scripts.run_daily_operator_agent import (
    receive_channel_health_ok,
    should_allow_daily_ask,
    should_stop_for_receive_channel,
    supplier_facing_outbox_exclusions,
)


def _wecom_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypted])).encode("utf-8")).hexdigest()


class CoreFlowTest(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            "os.environ",
            {"VISION_PROVIDER": "local", "GOOGLE_API_KEY": "", "OPENAI_API_KEY": ""},
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_report_and_selection_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "测试供应商", "张姐", "external_1", ["上衣/T恤"], "测试地址"))

            source = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#eeeeee").save(source)
            products = ingest_supplier_images(store, tmp_path, "S01", [source], datetime.fromisoformat("2026-05-21T10:00:00"))
            self.assertEqual(len(products), 1)

            report_dir = tmp_path / "reports"
            png, pdf, manifest = build_daily_report(store, "2026-05-21", report_dir)
            self.assertTrue(png.exists())
            self.assertTrue(pdf.exists())
            self.assertTrue(manifest.exists())

            selection = make_demo_selection(png, manifest, report_dir / "selection.png", limit=1)
            detected = detect_selections(manifest, selection)
            self.assertTrue(detected)
            self.assertEqual(detected[0].product_id, products[0].product_id)

    def test_selection_detects_colored_marker_and_text_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = tmp_path / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "page_width": 600,
                        "products": [
                            {"product_id": "SUP01-260524-01", "box": [80, 80, 320, 360]},
                            {"product_id": "SUP01-260524-02", "box": [340, 80, 580, 360]},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            screenshot = tmp_path / "blue_marker.png"
            marked = Image.new("RGB", (600, 440), "white")
            from PIL import ImageDraw

            ImageDraw.Draw(marked).rectangle((330, 70, 590, 370), outline="#1677ff", width=16)
            marked.save(screenshot)

            visual = detect_selections(manifest, screenshot, min_confidence=0.12)
            text = detect_selection_text(manifest, "这次先要 01 和 SUP01-260524-02")
            date_noise = detect_selection_text(manifest, "昨天 17:57\n2026-05-25\n这是今天的选款报表，请截图圈选")

        self.assertEqual(visual[0].product_id, "SUP01-260524-02")
        self.assertEqual({item.product_id for item in text}, {"SUP01-260524-01", "SUP01-260524-02"})
        self.assertEqual(date_noise, [])

    def test_report_collapses_detail_images_but_keeps_full_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "测试供应商", "张姐", "external_1", ["上衣/T恤"], "测试地址"))

            full_one = tmp_path / "blue_full_one.jpg"
            detail = tmp_path / "blue_detail.jpg"
            full_two = tmp_path / "blue_full_two.jpg"
            Image.new("RGB", (800, 1000), "#f6f3eb").save(full_one)
            with Image.open(full_one) as img:
                img.paste("#245fb6", (260, 170, 540, 860))
                img.save(full_one)
            Image.new("RGB", (800, 1000), "#2158ad").save(detail)
            Image.new("RGB", (800, 1000), "#f6f3eb").save(full_two)
            with Image.open(full_two) as img:
                img.paste("#245fb6", (220, 170, 500, 860))
                img.save(full_two)

            received = datetime.fromisoformat("2026-05-22T10:00:00")
            for idx, image in enumerate([full_one, detail, full_two], 1):
                product = Product(
                    product_id=f"S01-260522-0{idx}",
                    supplier_id="S01",
                    received_at=received.replace(second=idx),
                    primary_image=str(image),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(image)),
                    confidence=0.92,
                )
                store.upsert_product(product)

            _, _, manifest = build_daily_report(store, "2026-05-22", tmp_path / "reports")
            payload = json.loads(manifest.read_text(encoding="utf-8"))

            self.assertEqual(len(payload["products"]), 2)
            first = payload["products"][0]
            self.assertEqual(first["product_id"], "S01-260522-01")
            self.assertEqual(first["image_count"], 2)
            self.assertEqual(first["hidden_product_ids"], ["S01-260522-02"])
            self.assertEqual(payload["products"][1]["product_id"], "S01-260522-03")

    def test_style_role_handles_wechat_grid_border_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            detail = tmp_path / "wechat_blue_detail.jpg"
            full = tmp_path / "wechat_blue_full.jpg"

            Image.new("RGB", (400, 400), "#ffffff").save(detail)
            with Image.open(detail) as img:
                img.paste("#245fb6", (58, 72, 380, 392))
                img.paste("#d0a05a", (58, 72, 70, 392))
                img.save(detail)

            Image.new("RGB", (400, 400), "#ffffff").save(full)
            with Image.open(full) as img:
                img.paste("#d8ddd2", (58, 42, 380, 358))
                img.paste("#245fb6", (150, 88, 286, 330))
                img.save(full)

            self.assertEqual(classify_image_role(str(detail)), "detail")
            self.assertEqual(classify_image_role(str(full)), "full")

    def test_ai_group_splits_multiple_full_views_as_suspects(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blue_top = tmp_path / "blue_top.jpg"
            blue_sweater = tmp_path / "blue_sweater.jpg"
            Image.new("RGB", (500, 600), "#f2f2f2").save(blue_top)
            with Image.open(blue_top) as img:
                img.paste("#d8ddd2", (40, 40, 460, 560))
                img.paste("#245fb6", (160, 80, 340, 520))
                img.save(blue_top)
            Image.new("RGB", (500, 600), "#f2f2f2").save(blue_sweater)
            with Image.open(blue_sweater) as img:
                img.paste("#d8ddd2", (40, 40, 460, 560))
                img.paste("#245fb6", (150, 80, 350, 520))
                img.save(blue_sweater)

            received = datetime.fromisoformat("2026-05-22T10:00:00")
            products = [
                Product("S01-01", "S01", received, str(blue_top), [], "上衣", "T恤", phash(str(blue_top)), confidence=0.9),
                Product("S01-02", "S01", received, str(blue_sweater), [], "上衣", "卫衣", phash(str(blue_sweater)), confidence=0.9),
            ]
            raw_groups = [{"style_id": "S1", "representative_product_id": "S01-01", "product_ids": ["S01-01", "S01-02"]}]

            groups, suspects = _normalize_groups(products, raw_groups)
            cards = _cards_from_groups(products, groups, suspects)

            self.assertEqual(len(cards), 2)
            self.assertEqual({card.product.product_id for card in cards}, {"S01-01", "S01-02"})
            self.assertEqual({product_id for card in cards for product_id in card.suspect_product_ids}, {"S01-01", "S01-02"})

    def test_ai_group_collapses_single_full_with_detail_only(self):
        received = datetime.fromisoformat("2026-05-22T10:00:00")
        products = [
            Product("S01-01", "S01", received, "full.jpg", [], "上衣", "T恤", "p1", confidence=0.9),
            Product("S01-02", "S01", received, "detail.jpg", [], "上衣", "T恤", "p2", confidence=0.9),
        ]
        raw_groups = [{"style_id": "S1", "representative_product_id": "S01-02", "product_ids": ["S01-01", "S01-02"]}]

        with patch("supplier_bot.style_ai.classify_image_role", side_effect=lambda path: "detail" if "detail" in path else "full"):
            groups, suspects = _normalize_groups(products, raw_groups)
            cards = _cards_from_groups(products, groups, suspects)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].product.product_id, "S01-01")
        self.assertEqual([product.product_id for product in cards[0].detail_products], ["S01-02"])
        self.assertEqual(suspects, [])

    def test_demo_images_are_imported_as_distinct_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "测试供应商", "张姐", "external_1", ["上衣/T恤"], "测试地址"))

            demo_dir = tmp_path / "samples"
            make_demo_images(demo_dir)
            products = ingest_supplier_images(
                store,
                tmp_path,
                "S01",
                [demo_dir / "tee_white.jpg", demo_dir / "shirt_blue.jpg"],
                demo_received_at("2026-05-21"),
            )

            self.assertEqual([product.category_lv2 for product in products], ["T恤", "衬衫"])

    def test_product_ids_do_not_overwrite_across_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "测试供应商", "张姐", "external_1", ["上衣/T恤"], "测试地址"))

            demo_dir = tmp_path / "samples"
            make_demo_images(demo_dir)
            first_day = ingest_supplier_images(
                store,
                tmp_path,
                "S01",
                [demo_dir / "tee_white.jpg"],
                demo_received_at("2026-05-21"),
            )
            second_day = ingest_supplier_images(
                store,
                tmp_path,
                "S01",
                [demo_dir / "shirt_blue.jpg"],
                demo_received_at("2026-05-22"),
            )

            self.assertEqual(first_day[0].product_id, "S01-260521-01")
            self.assertEqual(second_day[0].product_id, "S01-260522-01")
            self.assertEqual(len(store.list_products()), 2)

    def test_same_image_can_be_reported_on_different_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "测试供应商", "张姐", "external_1", ["上衣/T恤"], "测试地址"))

            demo_dir = tmp_path / "samples"
            make_demo_images(demo_dir)
            first_day = ingest_supplier_images(
                store,
                tmp_path,
                "S01",
                [demo_dir / "tee_white.jpg"],
                demo_received_at("2026-05-21"),
            )
            second_day = ingest_supplier_images(
                store,
                tmp_path,
                "S01",
                [demo_dir / "tee_white.jpg"],
                demo_received_at("2026-05-22"),
            )

            self.assertEqual(first_day[0].product_id, "S01-260521-01")
            self.assertEqual(second_day[0].product_id, "S01-260522-01")
            self.assertEqual(len(store.list_products()), 2)

    def test_scheduler_respects_frequency_and_batch_size(self):
        suppliers = [
            Supplier("S01", "供应商一", "张姐", "external_1", ["上衣/T恤"], "地址"),
            Supplier("S02", "供应商二", "李姐", "external_2", ["上衣/衬衫"], "地址", send_frequency="weekdays"),
            Supplier("S03", "供应商三", "王姐", "external_3", ["下装/裤子"], "地址", paused=True),
        ]

        saturday = datetime.fromisoformat("2026-05-23T10:00:00").date()
        eligible = [supplier for supplier in suppliers if should_ask_supplier(supplier, saturday)]
        batches = build_batches(eligible, batch_size=1)

        self.assertEqual([[supplier.supplier_id for supplier in batch] for batch in batches], [["S01"]])

    def test_scheduler_skips_all_suppliers_on_sunday(self):
        suppliers = [
            Supplier("S01", "供应商一", "张姐", "external_1", ["上衣/T恤"], "地址"),
            Supplier("S02", "供应商二", "李姐", "external_2", ["上衣/衬衫"], "地址", send_frequency="weekdays"),
        ]

        sunday = datetime.fromisoformat("2026-05-31T10:00:00").date()

        self.assertTrue(is_supplier_rest_day(sunday))
        self.assertEqual([supplier.supplier_id for supplier in suppliers if should_ask_supplier(supplier, sunday)], [])

    def test_classifier_handles_shoes_and_supplier_category_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shoe = tmp_path / "black_shoes.jpg"
            tall_top = tmp_path / "gray_unknown.jpg"
            Image.new("RGB", (800, 800), "#eeeeee").save(shoe)
            Image.new("RGB", (900, 1400), "#eeeeee").save(tall_top)

            self.assertEqual(classify_image(str(shoe))[:2], ("鞋履", "单鞋"))
            result = classify_image_detail(str(tall_top), ["上衣/T恤"])
            self.assertNotEqual(result.category_lv1, "上衣")
            self.assertTrue(result.needs_review)

    def test_openai_vision_classifier_uses_image_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "unknown.jpg"
            Image.new("RGB", (800, 800), "#eeeeee").save(image)
            response = Mock()
            response.json.return_value = {
                "output": [
                    {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "category_lv1": "鞋履",
                                        "category_lv2": "单鞋",
                                        "confidence": 0.91,
                                        "attributes": {
                                            "color": "黑色",
                                            "style": "分趾平底鞋",
                                            "material": "皮革",
                                            "pattern": "纯色",
                                        },
                                        "reason": "图片主体是一双黑色鞋",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        ]
                    }
                ]
            }

            with patch.dict(
                "os.environ",
                {"VISION_PROVIDER": "openai", "OPENAI_API_KEY": "test-key", "OPENAI_VISION_MODEL": "gpt-5-mini"},
            ):
                with patch("requests.post", return_value=response) as post:
                    result = classify_image_detail(str(image), ["上衣/T恤"])

            self.assertEqual((result.category_lv1, result.category_lv2), ("鞋履", "单鞋"))
            self.assertFalse(result.needs_review)
            request = post.call_args.kwargs["json"]
            self.assertEqual(request["model"], "gpt-5-mini")
            self.assertEqual(request["input"][0]["content"][1]["type"], "input_image")
            self.assertEqual(request["text"]["format"]["type"], "json_schema")
            response.raise_for_status.assert_called_once()

    def test_gemini_vision_classifier_uses_inline_image_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "unknown.jpg"
            Image.new("RGB", (800, 800), "#eeeeee").save(image)
            response = Mock(status_code=200)
            response.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "category_lv1": "上衣",
                                            "category_lv2": "T恤",
                                            "confidence": 0.88,
                                            "attributes": {
                                                "color": "灰色",
                                                "style": "短袖",
                                                "material": "棉",
                                                "pattern": "纯色",
                                            },
                                            "reason": "图片主体是灰色短袖 T 恤",
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

            with patch.dict(
                "os.environ",
                {
                    "VISION_PROVIDER": "google",
                    "GOOGLE_API_KEY": "test-google-key",
                    "GOOGLE_VISION_MODEL": "gemini-2.5-flash",
                },
            ):
                with patch("requests.post", return_value=response) as post:
                    result = classify_image_detail(str(image), ["连体/连衣裙"])

            self.assertEqual((result.category_lv1, result.category_lv2), ("上衣", "T恤"))
            self.assertFalse(result.needs_review)
            self.assertIn("gemini-2.5-flash:generateContent", post.call_args.args[0])
            request = post.call_args.kwargs["json"]
            self.assertIn("inline_data", request["contents"][0]["parts"][0])
            self.assertEqual(request["generationConfig"]["responseMimeType"], "application/json")
            self.assertIn("responseSchema", request["generationConfig"])
            response.raise_for_status.assert_called_once()

    def test_gemini_style_calls_use_proxy_env(self):
        from supplier_bot.style_ai import _group_supplier_with_gemini

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "product.jpg"
            Image.new("RGB", (640, 800), "#eeeeee").save(image)
            product = Product(
                "SUP01-260524-01",
                "SUP01",
                datetime.fromisoformat("2026-05-25T10:00:00"),
                str(image),
                [],
                "上衣",
                "T恤",
                phash(str(image)),
            )
            style_response = Mock()
            style_response.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "groups": [
                                                {
                                                    "style_id": "S1",
                                                    "representative_product_id": product.product_id,
                                                    "product_ids": [product.product_id],
                                                }
                                            ],
                                            "suspect_groups": [],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

            with patch.dict(
                "os.environ",
                {
                    "GOOGLE_API_KEY": "test-google-key",
                    "GOOGLE_API_PROXY": "http://127.0.0.1:7890",
                    "GOOGLE_VISION_MODEL": "gemini-2.5-flash",
                },
            ):
                with patch("requests.post", return_value=style_response) as post:
                    _group_supplier_with_gemini("SUP01", [product], "test-google-key")

        self.assertEqual(post.call_args.kwargs["proxies"], {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})

    def test_env_file_loads_without_overriding_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("WECOM_DRY_RUN=0\nWECOM_WEBHOOK_URL=https://example.test/hook\n", encoding="utf-8")

            with patch.dict("os.environ", {"WECOM_DRY_RUN": "1"}, clear=True):
                load_env_file(env_path)
                config = Config()

            self.assertTrue(config.wecom_dry_run)
            self.assertEqual(config.wecom_webhook_url, "https://example.test/hook")

    def test_wecom_webhook_checks_api_error_code(self):
        config = Config()
        config.wecom_dry_run = False
        config.wecom_webhook_url = "https://example.test/hook"
        response = Mock()
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}

        with patch("requests.post", return_value=response) as post:
            payload = WeComClient(config).send_test_message("hello")

        self.assertEqual(payload, {"errcode": 0, "errmsg": "ok"})
        post.assert_called_once()
        response.raise_for_status.assert_called_once()

    def test_wecom_official_token_is_cached_and_app_text_posts_payload(self):
        config = Config()
        config.wecom_dry_run = False
        config.wecom_corp_id = "corp"
        config.wecom_agent_secret = "secret"
        config.wecom_agent_id = "1000002"

        token_response = Mock()
        token_response.json.return_value = {"errcode": 0, "access_token": "token", "expires_in": 7200}
        send_response = Mock()
        send_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

        with patch("requests.get", return_value=token_response) as get, patch("requests.post", return_value=send_response) as post:
            client = WeComClient(config)
            payload = client.send_app_text("OperatorUser", "hello")
            client.send_app_text("OperatorUser", "again")

        self.assertEqual(payload, {"errcode": 0, "errmsg": "ok"})
        get.assert_called_once()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["json"]["agentid"], 1000002)
        token_response.raise_for_status.assert_called_once()

    def test_wecom_message_archive_config_requires_secret_and_private_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk_path = Path(tmp) / "libWeWorkFinanceSdk_C.so"
            sdk_path.write_bytes(b"fake-sdk")
            config = Config()
            config.wecom_corp_id = "corp"
            config.wecom_msg_audit_secret = "secret"
            config.wecom_msg_audit_private_key_path = "/tmp/key.pem"
            config.wecom_msg_audit_sdk_lib = str(sdk_path)

            self.assertTrue(WeComClient(config).message_archive_configured())

            config.wecom_msg_audit_sdk_lib = ""
            self.assertFalse(WeComClient(config).message_archive_configured())

    def test_wecom_callback_server_verifies_get_and_decrypts_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config()
            config.data_dir = Path(tmp)
            config.wecom_callback_token = "callback-token"
            config.wecom_callback_encoding_aes_key = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"

            server = make_wecom_callback_server(config, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}/wecom/callback"
                timestamp = "1780380000"
                nonce = "nonce"
                echo = encrypt_callback_payload(config.wecom_callback_encoding_aes_key, "verified", config.wecom_corp_id)
                get_query = {
                    "msg_signature": _wecom_signature(config.wecom_callback_token, timestamp, nonce, echo),
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "echostr": echo,
                }
                with urlopen(f"{base_url}?{urlencode(get_query)}", timeout=5) as response:
                    self.assertEqual(response.read().decode("utf-8"), "verified")

                decrypted_xml = (
                    "<xml><ToUserName><![CDATA[corp]]></ToUserName><FromUserName><![CDATA[OperatorUser]]></FromUserName>"
                    "<CreateTime>1780380001</CreateTime><MsgType><![CDATA[event]]></MsgType>"
                    "<Event><![CDATA[subscribe]]></Event></xml>"
                )
                encrypted = encrypt_callback_payload(config.wecom_callback_encoding_aes_key, decrypted_xml, config.wecom_corp_id)
                post_query = {
                    "msg_signature": _wecom_signature(config.wecom_callback_token, timestamp, nonce, encrypted),
                    "timestamp": timestamp,
                    "nonce": nonce,
                }
                encrypted_xml = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
                request = Request(
                    f"{base_url}?{urlencode(post_query)}",
                    data=encrypted_xml.encode("utf-8"),
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.read().decode("utf-8"), "success")

                self.assertIn("<Event><![CDATA[subscribe]]></Event>", (config.data_dir / "wecom_callback_events/latest.xml").read_text(encoding="utf-8"))
                payload = json.loads((config.data_dir / "wecom_callback_events/latest.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["message"]["MsgType"], "event")
                self.assertEqual(payload["message"]["Event"], "subscribe")
            finally:
                server.shutdown()
                server.server_close()

    def test_daily_runner_does_not_catch_up_same_day_by_default(self):
        ask_at = datetime(2026, 5, 29, 9, 0).time()

        self.assertFalse(
            should_allow_daily_ask(
                datetime(2026, 5, 29, 16, 30),
                ask_at,
                datetime(2026, 5, 29, 16, 0),
            )
        )
        self.assertTrue(
            should_allow_daily_ask(
                datetime(2026, 5, 29, 16, 30),
                ask_at,
                datetime(2026, 5, 29, 16, 0),
                catch_up_today=True,
            )
        )
        self.assertTrue(
            should_allow_daily_ask(
                datetime(2026, 5, 29, 9, 0),
                ask_at,
                datetime(2026, 5, 29, 8, 0),
            )
        )
        self.assertTrue(
            should_allow_daily_ask(
                datetime(2026, 5, 30, 9, 0),
                ask_at,
                datetime(2026, 5, 29, 16, 0),
            )
        )

    def test_daily_runner_blocks_supplier_contact_on_sunday(self):
        ask_at = datetime(2026, 5, 31, 9, 0).time()

        self.assertFalse(
            should_allow_daily_ask(
                datetime(2026, 5, 31, 9, 0),
                ask_at,
                datetime(2026, 5, 31, 8, 0),
                catch_up_today=True,
            )
        )
        self.assertEqual(
            supplier_facing_outbox_exclusions(datetime(2026, 5, 31).date(), allow_ask=True),
            ["ask_supplier", "remind_supplier", "request_sample"],
        )
        self.assertEqual(
            supplier_facing_outbox_exclusions(
                datetime(2026, 6, 1).date(),
                allow_ask=True,
                allow_supplier_reminders=False,
            ),
            ["remind_supplier"],
        )
        self.assertEqual(
            supplier_facing_outbox_exclusions(
                datetime(2026, 6, 1).date(),
                allow_ask=False,
                allow_supplier_reminders=False,
            ),
            ["ask_supplier", "remind_supplier"],
        )

    def test_receive_channel_health_blocks_stale_or_failed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime_dir = tmp_path / "data" / "runtime"
            runtime_dir.mkdir(parents=True)
            health_path = runtime_dir / "wecom_archive_health.json"
            old_data_dir = config.data_dir
            config.data_dir = tmp_path / "data"
            try:
                health_path.write_text(
                    json.dumps(
                        {
                            "ok": False,
                            "source": "server_wecom_archive",
                            "detail": "missing WECOM_MSG_AUDIT_SDK_LIB",
                            "checked_at": "2026-06-01T13:57:20",
                            "date": "2026-06-01",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                ok, detail = receive_channel_health_ok(
                    datetime(2026, 6, 1).date(),
                    now=datetime.fromisoformat("2026-06-01T14:07:20"),
                )
                self.assertFalse(ok)
                self.assertIn("missing WECOM_MSG_AUDIT_SDK_LIB", detail)

                health_path.write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "source": "server_wecom_archive",
                            "detail": "官方收图和入库本轮成功",
                            "checked_at": "2026-06-01T13:00:00",
                            "date": "2026-06-01",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                ok, detail = receive_channel_health_ok(
                    datetime(2026, 6, 1).date(),
                    now=datetime.fromisoformat("2026-06-01T14:07:20"),
                )
                self.assertFalse(ok)
                self.assertIn("过期", detail)

                health_path.write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "source": "server_wecom_archive",
                            "detail": "官方收图和入库本轮成功",
                            "checked_at": "2026-06-01T14:00:00",
                            "date": "2026-06-01",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                ok, detail = receive_channel_health_ok(
                    datetime(2026, 6, 1).date(),
                    now=datetime.fromisoformat("2026-06-01T14:07:20"),
                )
                self.assertTrue(ok)
            finally:
                config.data_dir = old_data_dir

    def test_official_modes_stop_when_receive_channel_is_unhealthy(self):
        self.assertTrue(should_stop_for_receive_channel("official", False))
        self.assertTrue(should_stop_for_receive_channel("hybrid", False))
        self.assertFalse(should_stop_for_receive_channel("official", True))
        self.assertFalse(should_stop_for_receive_channel("hybrid", True))
        self.assertFalse(should_stop_for_receive_channel("desktop", False))

    def test_alert_email_requires_smtp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            alert_config = Config()
            alert_config.data_dir = Path(tmp) / "data"
            alert_config.alert_email_to = ""
            alert_config.smtp_host = ""

            result = send_receive_channel_failure_alert(alert_config, "missing SDK")

        self.assertFalse(result.sent)
        self.assertTrue(result.skipped)
        self.assertIn("邮件报警未配置", result.detail)

    def test_alert_email_sends_and_deduplicates(self):
        sent_messages = []

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def login(self, username, password):
                self.username = username
                self.password = password

            def send_message(self, message):
                sent_messages.append(message)

        with tempfile.TemporaryDirectory() as tmp:
            alert_config = Config()
            alert_config.data_dir = Path(tmp) / "data"
            alert_config.alert_email_enabled = True
            alert_config.alert_email_to = "ares@example.com"
            alert_config.alert_email_from = "bot@example.com"
            alert_config.smtp_host = "smtp.example.com"
            alert_config.smtp_port = 465
            alert_config.smtp_username = "bot@example.com"
            alert_config.smtp_password = "secret"
            alert_config.smtp_use_ssl = True
            alert_config.alert_email_cooldown_seconds = 3600

            with patch("supplier_bot.alerts.smtplib.SMTP_SSL", FakeSMTP):
                first = send_alert_email(
                    alert_config,
                    "SDK failed",
                    "missing WECOM_MSG_AUDIT_SDK_LIB",
                    category="receive_channel_failure",
                    now=datetime.fromisoformat("2026-06-01T09:00:00"),
                )
                second = send_alert_email(
                    alert_config,
                    "SDK failed",
                    "missing WECOM_MSG_AUDIT_SDK_LIB",
                    category="receive_channel_failure",
                    now=datetime.fromisoformat("2026-06-01T09:10:00"),
                )

        self.assertTrue(first.sent)
        self.assertFalse(second.sent)
        self.assertTrue(second.skipped)
        self.assertEqual(len(sent_messages), 1)
        self.assertEqual(sent_messages[0]["To"], "ares@example.com")

    def test_receive_failure_diagnostics_and_recovery_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recovery_config = Config()
            recovery_config.data_dir = tmp_path / "data"
            recovery_config.db_path = recovery_config.data_dir / "bot.sqlite3"
            recovery_config.runtime_mode = "hybrid"
            recovery_config.wecom_msg_audit_sdk_lib = ""
            recovery_config.wecom_msg_audit_secret = "audit"
            recovery_config.wecom_msg_audit_private_key_path = "/tmp/key.pem"
            recovery_config.data_dir.mkdir(parents=True)
            write_contact_roles(
                recovery_config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "测试供应商", external_user_id="wm_supplier", roles=[ROLE_SUPPLIER]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )

            diagnostics = diagnose_receive_channel(
                recovery_config,
                datetime(2026, 6, 1).date(),
                "missing WECOM_MSG_AUDIT_SDK_LIB",
                project_root=tmp_path,
            )
            failure_path = record_receive_channel_failure(
                recovery_config,
                datetime(2026, 6, 1).date(),
                "missing WECOM_MSG_AUDIT_SDK_LIB",
                diagnostics,
            )

            self.assertTrue(failure_path.exists())
            self.assertTrue(receive_recovery_required(recovery_config, datetime(2026, 6, 1).date()))
            self.assertIn("SDK", " ".join(diagnostics["suspected_causes"]))

    def test_receive_recovery_reconciliation_blocks_unprocessed_or_unknown_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recovery_config = Config()
            recovery_config.data_dir = tmp_path / "data"
            recovery_config.db_path = recovery_config.data_dir / "bot.sqlite3"
            recovery_config.data_dir.mkdir(parents=True)
            write_contact_roles(
                recovery_config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "测试供应商", external_user_id="wm_supplier", roles=[ROLE_SUPPLIER]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            store = Store(recovery_config.db_path)
            store.init()
            run_date = datetime(2026, 6, 1).date()
            pending_dir = recovery_config.data_dir / "inbox_events" / "pending"
            pending_dir.mkdir(parents=True)
            (pending_dir / "evt.json").write_text(
                json.dumps(
                    {
                        "event_id": "evt",
                        "supplier_id": "SUP01",
                        "received_at": "2026-06-01T10:00:00",
                        "image_paths": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            blocked = reconcile_receive_recovery(recovery_config, store, run_date)
            self.assertFalse(blocked.ok)
            self.assertIn("未处理", blocked.detail)

            (pending_dir / "evt.json").unlink()
            unknown_dir = recovery_config.data_dir / "archive_unknown" / run_date.isoformat()
            unknown_dir.mkdir(parents=True)
            (unknown_dir / "unknown.json").write_text("{}", encoding="utf-8")
            blocked_unknown = reconcile_receive_recovery(recovery_config, store, run_date)
            self.assertFalse(blocked_unknown.ok)
            self.assertIn("未绑定", blocked_unknown.detail)

            (unknown_dir / "unknown.json").unlink()
            ok = reconcile_receive_recovery(recovery_config, store, run_date)
            self.assertTrue(ok.ok)

    def test_receive_recovery_record_clears_required_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recovery_config = Config()
            recovery_config.data_dir = tmp_path / "data"
            recovery_config.db_path = recovery_config.data_dir / "bot.sqlite3"
            recovery_config.data_dir.mkdir(parents=True)
            write_contact_roles(
                recovery_config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "测试供应商", external_user_id="wm_supplier", roles=[ROLE_SUPPLIER]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            store = Store(recovery_config.db_path)
            store.init()
            run_date = datetime(2026, 6, 1).date()
            record_receive_channel_failure(recovery_config, run_date, "health stale", {"suspected_causes": ["stale"]})
            reconciliation = reconcile_receive_recovery(recovery_config, store, run_date)
            record_receive_channel_recovery(
                recovery_config,
                run_date,
                "官方收图和入库本轮成功",
                {"processed": 0, "failed": 0},
                reconciliation,
            )

            self.assertFalse(receive_recovery_required(recovery_config, run_date))

    def test_workflow_report_ignores_products_from_disabled_suppliers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workflow_config = Config()
            workflow_config.data_dir = tmp_path / "data"
            workflow_config.db_path = workflow_config.data_dir / "bot.sqlite3"
            workflow_config.vision_provider = "local"
            workflow_config.data_dir.mkdir(parents=True)
            write_contact_roles(
                workflow_config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP_DEMO_A", "示例供应商甲", external_user_id="", roles=[ROLE_SUPPLIER], enabled=True),
                    ContactRole("OLD_SUPPLIER", "旧供应商", external_user_id="wm_old", roles=[ROLE_SUPPLIER], enabled=False),
                    ContactRole("SELECTOR_A", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("OPERATOR_A", "运营甲", roles=[ROLE_OPERATOR]),
                ],
            )
            store = Store(workflow_config.db_path)
            store.init()
            store.upsert_supplier(Supplier("OLD_SUPPLIER", "旧供应商", "旧供应商", "wm_old", [], ""))
            image = tmp_path / "old.jpg"
            Image.new("RGB", (500, 700), "#eeeeee").save(image)
            store.upsert_product(
                Product(
                    "OLD_SUPPLIER-260601-01",
                    "OLD_SUPPLIER",
                    datetime.fromisoformat("2026-06-01T10:00:00"),
                    str(image),
                    [],
                    "上衣",
                    "T恤",
                    "oldhash",
                    confidence=0.9,
                )
            )

            result = WorkflowEngine(workflow_config, store).run_once(
                datetime(2026, 6, 1).date(),
                now=datetime.fromisoformat("2026-06-01T16:00:00"),
            )
            workflow = load_daily_workflow(workflow_config.data_dir / "tasks" / "2026-06-01" / "daily_workflow.json")

            self.assertFalse(workflow.report_path)
            self.assertNotIn("report_ready", " ".join(result.actions))

    def test_contact_roles_roundtrip_and_supplier_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wecom_contacts.json"
            contacts = [
                ContactRole(
                    contact_id="external_1",
                    display_name="张三供应商",
                    external_user_id="external_1",
                    roles=[ROLE_SUPPLIER, ROLE_OPERATOR],
                    main_categories=["上衣/T恤"],
                    sample_address="测试地址",
                ),
                ContactRole("ares", "OperatorUser", roles=[ROLE_CONFIRMER, ROLE_OPERATOR, ROLE_SELECTOR]),
            ]
            write_contact_roles(path, contacts)

            reloaded = load_contact_roles(path)
            suppliers = contacts_to_suppliers(reloaded)

        self.assertEqual(len(suppliers), 1)
        self.assertEqual(suppliers[0].name, "张三供应商")
        self.assertEqual(suppliers[0].main_categories, ["上衣/T恤"])
        self.assertEqual([item.display_name for item in contacts_by_role(reloaded, ROLE_SELECTOR)], ["OperatorUser"])
        self.assertEqual([item.display_name for item in contacts_by_role(reloaded, ROLE_CONFIRMER)], ["OperatorUser"])

    def test_contact_role_merge_keeps_manual_roles(self):
        existing = [ContactRole("external_1", "旧名称", roles=[ROLE_SUPPLIER], sample_address="地址")]
        incoming = [ContactRole("external_1", "新名称", external_user_id="external_1")]

        merged = merge_contact_roles(existing, incoming)

        self.assertEqual(merged[0].display_name, "新名称")
        self.assertEqual(merged[0].roles, [ROLE_SUPPLIER])
        self.assertEqual(merged[0].sample_address, "地址")

    def test_contact_role_merge_binds_official_contact_by_name(self):
        existing = [ContactRole("SUP_DEMO_C", "示例供应商丙", roles=[ROLE_SUPPLIER], source="manual")]
        incoming = [ContactRole("wo_real", "示例供应商丙", external_user_id="wo_real", source="wecom_external_contact")]

        merged = merge_contact_roles(existing, incoming)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].contact_id, "SUP_DEMO_C")
        self.assertEqual(merged[0].external_user_id, "wo_real")
        self.assertEqual(merged[0].roles, [ROLE_SUPPLIER])

    def test_disabled_contacts_are_not_used_for_archive_lookup(self):
        contacts = [
            ContactRole("SUP_DISABLED", "停用供应商", external_user_id="wm_disabled", roles=[ROLE_SUPPLIER], enabled=False),
            ContactRole("SUP_ACTIVE", "启用供应商", external_user_id="wm_active", roles=[ROLE_SUPPLIER], enabled=True),
        ]

        lookup = _contact_lookup(contacts)

        self.assertNotIn("wm_disabled", lookup)
        self.assertNotIn("停用供应商", lookup)
        self.assertEqual(lookup["wm_active"].contact_id, "SUP_ACTIVE")

    def test_auto_bind_supplier_from_unknown_archive_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            contacts_path = data_dir / "wecom_contacts.json"
            write_contact_roles(
                contacts_path,
                [ContactRole("SUP01", "示例供应商丁", roles=[ROLE_SUPPLIER])],
            )
            unknown_dir = data_dir / "archive_unknown" / "2026-05-30"
            unknown_dir.mkdir(parents=True)
            (unknown_dir / "msg.json").write_text(
                json.dumps(
                    {
                        "sender": "wm_supplier_real",
                        "text": "我是示例供应商丁，今天新款发你",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            changed = auto_bind_contacts_from_unknown_archive(contacts_path, data_dir)
            contacts = load_contact_roles(contacts_path)

        self.assertEqual([contact.display_name for contact in changed], ["示例供应商丁"])
        self.assertEqual(contacts[0].external_user_id, "wm_supplier_real")

    def test_contact_from_external_payload_uses_remark_first(self):
        contact = contact_from_external_payload(
            {
                "external_contact": {"external_userid": "wm_abc", "name": "微信名", "corp_name": "公司"},
                "follow_info": {"remark": "供应商备注名", "userid": "OperatorUser"},
            }
        )

        self.assertEqual(contact.contact_id, "wm_abc")
        self.assertEqual(contact.display_name, "供应商备注名")
        self.assertEqual(contact.owner_userid, "OperatorUser")

    def test_wecom_sync_external_contacts_uses_customer_contact_api(self):
        config = Config()
        config.wecom_dry_run = False
        config.wecom_corp_id = "corp"
        config.wecom_agent_secret = "secret"

        token_response = Mock()
        token_response.json.return_value = {"errcode": 0, "access_token": "token", "expires_in": 7200}
        list_response = Mock()
        list_response.json.return_value = {"errcode": 0, "external_userid": ["wm_abc"]}
        get_response = Mock()
        get_response.json.return_value = {
            "errcode": 0,
            "external_contact": {"external_userid": "wm_abc", "name": "微信名"},
            "follow_info": {"remark": "供应商备注名", "userid": "OperatorUser"},
        }

        with patch("requests.get", side_effect=[token_response, list_response, get_response]) as get:
            contacts = WeComClient(config).sync_external_contacts(["OperatorUser"])

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].display_name, "供应商备注名")
        self.assertEqual(get.call_count, 3)

    def test_daily_workflow_initializes_from_roles_and_preserves_status(self):
        suppliers = [
            Supplier("S01", "供应商一", "张姐", "external_1", ["上衣/T恤"], "地址"),
            Supplier("S02", "供应商二", "李姐", "external_2", ["鞋履/单鞋"], "地址"),
        ]
        selectors = [ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR])]
        operators = [ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR])]

        workflow = initialize_daily_workflow("2026-05-25", suppliers, selectors, operators)
        advance_supplier_status(workflow, "S01", STATUS_ASK_SENT, sent_at="2026-05-25T09:00:00")
        reloaded = initialize_daily_workflow("2026-05-25", suppliers, selectors, operators, existing=workflow)

        self.assertEqual(workflow_summary(reloaded)[STATUS_PENDING_ASK], 1)
        self.assertEqual([item.supplier_id for item in suppliers_needing_ask(reloaded)], ["S02"])
        self.assertEqual(reloaded.suppliers[0].status, STATUS_ASK_SENT)
        self.assertEqual(reloaded.selectors[0].display_name, "选款人甲")

    def test_daily_workflow_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_workflow.json"
            workflow = initialize_daily_workflow(
                "2026-05-25",
                [Supplier("S01", "供应商一", "张姐", "external_1", ["上衣/T恤"], "地址")],
                [],
                [],
            )
            write_daily_workflow(path, workflow)
            loaded = load_daily_workflow(path)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.suppliers[0].supplier_id, "S01")

    def test_build_desktop_question_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "杭州初白服饰", "张姐", "external_1", ["上衣/T恤"], "测试地址"))
            store.upsert_supplier(Supplier("S02", "广州织夏档口", "陈生", "external_2", ["连体/连衣裙"], "测试地址"))

            tasks = build_daily_question_plan(store, "2026-05-22", batch_size=10, batch_index=0, supplier_ids=["S02"])
            output = write_desktop_plan(tasks, tmp_path / "tasks.json")

            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].search_text, "广州织夏档口")
            self.assertEqual(tasks[0].message, "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。\n今日批次：0522-S02")
            self.assertTrue(output.exists())

            all_tasks = build_daily_question_plan(store, "2026-05-22", batch_size=10, batch_index=0)
            self.assertEqual(
                {task.supplier_id: task.message.rsplit("：", 1)[-1] for task in all_tasks},
                {"S01": "0522-S01", "S02": "0522-S02"},
            )

            reloaded = load_tasks(output)
            marked = mark_tasks_sent(reloaded, datetime.fromisoformat("2026-05-22T15:00:00"))
            self.assertEqual(marked[0].status, "waiting_reply")
            self.assertEqual(marked[0].sent_at, "2026-05-22T15:00:00")
            self.assertEqual(len(pending_reply_tasks(marked)), 1)

    def test_sample_request_tasks_enrich_minimal_selection_from_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "SUP01-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": "data/inbox/SUP01-01.jpg",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            screenshot = report_dir / "selector_marked.png"
            Image.new("RGB", (500, 500), "#ffffff").save(screenshot)
            selection_path = report_dir / "selection.json"
            selection_path.write_text(
                json.dumps(
                    [{"product_id": "SUP01-01", "selected_variant": "右侧黄色", "screenshot_path": str(screenshot)}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = build_sample_request_tasks(selection_path, report_dir / "sample_request_tasks.json")
            tasks = load_tasks(output)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].supplier_id, "SUP01")
        self.assertNotIn("SUP01-01", tasks[0].message)
        self.assertNotIn("右侧黄色", tasks[0].message)
        self.assertNotIn("选款人甲", tasks[0].message)
        self.assertIn("货号/款号", tasks[0].message)
        self.assertEqual(tasks[0].attachments, [str(screenshot)])

    def test_sample_request_blocks_historical_desktop_selector_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "SUP01-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "primary_image": "data/inbox/SUP01-01.jpg",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            screenshot = report_dir / "old_selector_marked.png"
            Image.new("RGB", (500, 500), "#ffffff").save(screenshot)
            selection_path = report_dir / "selection.json"
            selection_path.write_text(
                json.dumps(
                    [
                        {
                            "product_id": "SUP01-01",
                            "source": "desktop_selector_capture",
                            "screenshot_path": str(screenshot),
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = build_sample_request_tasks(selection_path, report_dir / "sample_request_tasks.json")
            tasks = load_tasks(output)

        self.assertEqual(tasks[0].status, "needs_selection_screenshot")
        self.assertEqual(tasks[0].attachments, [])
        self.assertIn("今天报表消息之后", tasks[0].notes)

    def test_sample_request_crops_selected_supplier_cards_before_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "page_width": 500,
                        "products": [
                            {
                                "product_id": "SUP01-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "供应商一",
                                "primary_image": "data/inbox/SUP01-01.jpg",
                                "box": [50, 50, 200, 220],
                            },
                            {
                                "product_id": "SUP02-01",
                                "supplier_id": "SUP02",
                                "supplier_name": "供应商二",
                                "primary_image": "data/inbox/SUP02-01.jpg",
                                "box": [260, 50, 430, 220],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            screenshot = report_dir / "selector_marked.png"
            image = Image.new("RGB", (500, 320), "#ffffff")
            image.paste("#d92323", (50, 50, 200, 220))
            image.paste("#245fb6", (260, 50, 430, 220))
            image.save(screenshot)
            selection_path = report_dir / "selection.json"
            selection_path.write_text(
                json.dumps(
                    [{"product_id": "SUP01-01", "source": "manual", "screenshot_path": str(screenshot)}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = build_sample_request_tasks(selection_path, report_dir / "sample_request_tasks.json")
            task = load_tasks(output)[0]
            safe_path = Path(task.attachments[0])
            safe_exists = safe_path.exists()
            with Image.open(safe_path) as safe_image:
                safe_rgb = safe_image.convert("RGB")
                safe_size = safe_rgb.size
                redish_pixels = sum(1 for r, g, b in safe_rgb.getdata() if r > 150 and g < 80 and b < 80)
                blueish_pixels = sum(1 for r, g, b in safe_rgb.getdata() if b > 130 and r < 120 and g < 150)

        self.assertEqual(task.status, "pending")
        self.assertNotEqual(safe_path, screenshot)
        self.assertTrue(safe_exists)
        self.assertLess(safe_size[0], 500)
        self.assertLess(safe_size[1], 320)
        self.assertGreater(redish_pixels, 1000)
        self.assertEqual(blueish_pixels, 0)

    def test_selection_waits_for_quiet_window_before_sample_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            selection_path = Path(tmp) / "selection.json"
            selection_path.write_text(
                json.dumps(
                    [
                        {
                            "product_id": "SUP01-01",
                            "selected_at": "2026-05-25T15:00:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            waiting = _selection_sample_request_readiness(
                selection_path,
                datetime.fromisoformat("2026-05-25T15:05:00"),
                10,
                day_time(18, 0),
            )
            ready = _selection_sample_request_readiness(
                selection_path,
                datetime.fromisoformat("2026-05-25T15:10:01"),
                10,
                day_time(18, 0),
            )

        self.assertFalse(waiting["ready"])
        self.assertEqual(waiting["reason"], "quiet_window")
        self.assertTrue(ready["ready"])

    def test_selection_after_cutoff_blocks_auto_sample_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            selection_path = Path(tmp) / "selection.json"
            selection_path.write_text(
                json.dumps([{"product_id": "SUP01-01", "selected_at": "2026-05-25T17:55:00"}], ensure_ascii=False),
                encoding="utf-8",
            )

            readiness = _selection_sample_request_readiness(
                selection_path,
                datetime.fromisoformat("2026-05-25T18:06:00"),
                10,
                day_time(18, 0),
            )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["reason"], "too_late_for_auto_sample_request")

    def test_ops_table_embeds_selected_images_and_supplier_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openpyxl import load_workbook

            tmp_path = Path(tmp)
            image_path = tmp_path / "images" / "SUP01-01.jpg"
            image_path.parent.mkdir()
            Image.new("RGB", (800, 1000), "#f5d34c").save(image_path)

            report_dir = tmp_path / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "SUP01-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": "images/SUP01-01.jpg",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            selection_path = report_dir / "selection.json"
            selection_path.write_text(
                json.dumps([{"product_id": "SUP01-01", "selected_variant": "黄色"}], ensure_ascii=False),
                encoding="utf-8",
            )
            replies_path = report_dir / "supplier_replies.json"
            replies_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "product_id": "SUP01-01",
                                "status": "product_info_received",
                                "style_no": "KZ26052401",
                                "color": "黄色",
                                "sizes": "S/M/L",
                                "material": "棉",
                                "price": "129",
                                "stock_or_moq": "现货 30 件",
                                "lead_time": "48 小时",
                                "raw_reply": "黄色有货，可以寄样。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = build_ops_table(selection_path, replies_path, report_dir / "ops.xlsx", root_dir=tmp_path)
            workbook = load_workbook(output)
            sheet = workbook["选款商品信息"]

        self.assertEqual(sheet["B5"].value, "SUP01-01")
        self.assertEqual(sheet["F5"].value, "KZ26052401")
        self.assertEqual(sheet["M5"].value, "信息已回")
        self.assertEqual(len(sheet._images), 1)

    def test_workflow_engine_runs_report_selection_sample_and_ops_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.wecom_dry_run = True
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "测试供应商", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"], sample_address="地址"),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            image_path = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#ececec").save(image_path)
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T10:00:00"),
                    primary_image=str(image_path),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(image_path)),
                    confidence=0.91,
                )
            )
            engine = WorkflowEngine(config, store)

            first = engine.run_once(datetime.fromisoformat("2026-05-25T00:00:00").date(), use_ai_style=False)
            outbox_path = config.data_dir / "tasks" / "2026-05-25" / "desktop_outbox.json"
            ask_tasks = load_outbox(outbox_path)
            report_dir = config.data_dir / "reports" / "2026-05-25"
            selection_path = report_dir / "selection.json"
            screenshot = report_dir / "selector_marked.png"
            Image.new("RGB", (500, 500), "#ffffff").save(screenshot)
            selection_path.write_text(
                json.dumps(
                    [
                        {
                            "product_id": "SUP01-260524-01",
                            "selected_variant": "米白色",
                            "screenshot_path": str(screenshot),
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            second = engine.run_once(
                datetime.fromisoformat("2026-05-25T00:00:00").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T15:30:00"),
            )
            workflow_path = config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json"
            workflow = load_daily_workflow(workflow_path)
            self.assertEqual(workflow.suppliers[0].status, STATUS_SELECTION_RECEIVED)
            replies_path = report_dir / "supplier_replies.json"
            replies_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "product_id": "SUP01-260524-01",
                                "status": "product_info_received",
                                "style_no": "T26052401",
                                "color": "米白色",
                                "sizes": "S/M/L",
                                "material": "棉",
                                "price": "99",
                                "stock_or_moq": "现货",
                                "lead_time": "明天",
                                "raw_reply": "可以寄样，信息如下。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            third = engine.run_once(datetime.fromisoformat("2026-05-25T00:00:00").date(), use_ai_style=False)
            all_outbox_tasks = load_outbox(outbox_path)

            self.assertIn("report_ready", " ".join(first.actions))
            self.assertIn("sample_request_plan", " ".join(second.actions))
            self.assertIn("sample_outbox", " ".join(second.actions))
            self.assertIn("ops_table", " ".join(third.actions))
            self.assertTrue((config.data_dir / "tasks" / "2026-05-25" / "sample_request_tasks.json").exists())
            self.assertTrue((report_dir / "ops_selected_product_info.xlsx").exists())
            self.assertEqual(store.get_product("SUP01-260524-01").status, ProductStatus.SUPPLIER_CONFIRMED)
            self.assertEqual(ask_tasks[0].kind, "ask_supplier")
            self.assertIn("report:2026-05-25:selector_1", {task.task_id for task in all_outbox_tasks})
            self.assertIn("sample:2026-05-25:SUP01", {task.task_id for task in all_outbox_tasks})
            self.assertIn("ops:2026-05-25:operator_1", {task.task_id for task in all_outbox_tasks})

    def test_workflow_engine_waits_for_all_suppliers_or_cutoff_before_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.supplier_reminder_time = "14:00"
            config.report_finalize_time = "15:00"
            config.supplier_image_quiet_minutes = 10
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "供应商一", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"]),
                    ContactRole("SUP02", "供应商二", roles=[ROLE_SUPPLIER], main_categories=["鞋履/单鞋"]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                ],
            )
            image_path = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#ececec").save(image_path)
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T10:00:00"),
                    primary_image=str(image_path),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(image_path)),
                    confidence=0.91,
                )
            )
            engine = WorkflowEngine(config, store)

            morning = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T10:30:00"),
            )
            morning_workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")
            after_reminder = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T14:01:00"),
            )
            afternoon = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T15:01:00"),
            )
            afternoon_workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")

        self.assertIn("report_waiting_for_more_supplier_images", morning.actions)
        self.assertEqual(morning_workflow.report_path, "")
        self.assertIn("report_waiting_for_more_supplier_images", after_reminder.actions)
        self.assertIn("report_ready", " ".join(afternoon.actions))
        self.assertTrue(afternoon_workflow.report_path)

    def test_workflow_engine_waits_for_supplier_image_quiet_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.supplier_reminder_time = "14:00"
            config.report_finalize_time = "15:00"
            config.supplier_image_quiet_minutes = 10
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "供应商一", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                ],
            )
            image_path = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#ececec").save(image_path)
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T10:00:00"),
                    primary_image=str(image_path),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(image_path)),
                    confidence=0.91,
                )
            )
            engine = WorkflowEngine(config, store)

            still_sending = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T10:05:00"),
            )
            quiet = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T10:11:00"),
            )
            workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")

        self.assertIn("report_waiting_for_more_supplier_images", still_sending.actions)
        self.assertIn("report_ready", " ".join(quiet.actions))
        self.assertTrue(workflow.report_path)

    def test_workflow_engine_cutoff_does_not_skip_recent_supplier_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.supplier_reminder_time = "14:00"
            config.report_finalize_time = "15:00"
            config.supplier_image_quiet_minutes = 10
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "供应商一", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"]),
                    ContactRole("SUP02", "供应商二", roles=[ROLE_SUPPLIER], main_categories=["鞋履/单鞋"]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                ],
            )
            image_path = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#ececec").save(image_path)
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T13:56:00"),
                    primary_image=str(image_path),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(image_path)),
                    confidence=0.91,
                )
            )
            engine = WorkflowEngine(config, store)

            cutoff_but_recent = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T14:01:00"),
            )
            quiet_after_reminder = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T14:07:00"),
            )
            after_finalize = engine.run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T15:01:00"),
            )
            workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")

        self.assertIn("report_waiting_for_more_supplier_images", cutoff_but_recent.actions)
        self.assertIn("report_waiting_for_more_supplier_images", quiet_after_reminder.actions)
        self.assertIn("report_ready", " ".join(after_finalize.actions))
        self.assertTrue(workflow.report_path)

    def test_workflow_engine_cutoff_reminds_only_suppliers_without_any_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.supplier_reminder_time = "14:00"
            config.report_finalize_time = "15:00"
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "无回复供应商", external_user_id="wm_sup01", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"]),
                    ContactRole("SUP02", "无新款供应商", external_user_id="wm_sup02", roles=[ROLE_SUPPLIER], main_categories=["鞋履/单鞋"]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            engine = WorkflowEngine(config, store)
            engine.run_once(datetime.fromisoformat("2026-05-25").date(), now=datetime.fromisoformat("2026-05-25T09:00:00"))
            outbox_path = config.data_dir / "tasks" / "2026-05-25" / "desktop_outbox.json"
            mark_outbox_sent(
                outbox_path,
                ["ask:2026-05-25:SUP01", "ask:2026-05-25:SUP02"],
                sent_at=datetime.fromisoformat("2026-05-25T09:01:00"),
            )
            engine.run_once(datetime.fromisoformat("2026-05-25").date(), now=datetime.fromisoformat("2026-05-25T09:02:00"))
            workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")
            no_new_flow = next(item for item in workflow.suppliers if item.supplier_id == "SUP02")
            no_new_flow.last_text_reply_at = "2026-05-25T09:10:00"
            no_new_flow.last_text_reply = "今天没有新款"
            no_new_flow.morning_reply_kind = "no_new"
            write_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json", workflow)

            result = engine.run_once(datetime.fromisoformat("2026-05-25").date(), now=datetime.fromisoformat("2026-05-25T14:01:00"))
            task_ids = {task.task_id for task in load_outbox(outbox_path)}

        self.assertIn("cutoff_reminder_outbox", " ".join(result.actions))
        self.assertIn("reminder:2026-05-25:SUP01", task_ids)
        self.assertNotIn("reminder:2026-05-25:SUP02", task_ids)

    def test_workflow_engine_blocks_reminder_when_supplier_external_id_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.supplier_reminder_time = "14:00"
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [ContactRole("SUP01", "未绑定供应商", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"])],
            )
            engine = WorkflowEngine(config, store)
            run_date = datetime.fromisoformat("2026-05-25").date()
            engine.run_once(run_date, now=datetime.fromisoformat("2026-05-25T09:00:00"))
            outbox_path = config.data_dir / "tasks" / "2026-05-25" / "desktop_outbox.json"
            mark_outbox_sent(
                outbox_path,
                ["ask:2026-05-25:SUP01"],
                sent_at=datetime.fromisoformat("2026-05-25T09:01:00"),
            )
            engine.run_once(run_date, now=datetime.fromisoformat("2026-05-25T09:02:00"))

            result = engine.run_once(run_date, now=datetime.fromisoformat("2026-05-25T14:01:00"))
            task_ids = {task.task_id for task in load_outbox(outbox_path)}
            approvals = json.loads((config.data_dir / "tasks" / "2026-05-25" / "pending_approvals.json").read_text(encoding="utf-8"))["items"]

        self.assertNotIn("cutoff_reminder_outbox", " ".join(result.actions))
        self.assertNotIn("reminder:2026-05-25:SUP01", task_ids)
        self.assertEqual(approvals[0]["approval_id"], "reminder:SUP01:missing_external_userid")

    def test_workflow_engine_blocks_reminders_when_receive_channel_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.supplier_reminder_time = "14:00"
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [ContactRole("SUP01", "无回复供应商", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"])],
            )
            engine = WorkflowEngine(config, store)
            run_date = datetime.fromisoformat("2026-05-25").date()
            engine.run_once(run_date, now=datetime.fromisoformat("2026-05-25T09:00:00"))
            outbox_path = config.data_dir / "tasks" / "2026-05-25" / "desktop_outbox.json"
            mark_outbox_sent(
                outbox_path,
                ["ask:2026-05-25:SUP01"],
                sent_at=datetime.fromisoformat("2026-05-25T09:01:00"),
            )
            engine.run_once(run_date, now=datetime.fromisoformat("2026-05-25T09:02:00"))

            result = engine.run_once(
                run_date,
                now=datetime.fromisoformat("2026-05-25T14:01:00"),
                allow_supplier_reminders=False,
            )
            task_ids = {task.task_id for task in load_outbox(outbox_path)}

        self.assertIn("supplier_reminders_blocked_receive_channel_disabled", result.actions)
        self.assertNotIn("reminder:2026-05-25:SUP01", task_ids)

    def test_attention_outbox_prefers_confirmer_over_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("operator_1", "运营甲", roles=[ROLE_OPERATOR]),
                    ContactRole("confirmer_1", "OperatorUser", roles=[ROLE_CONFIRMER]),
                ],
            )
            engine = WorkflowEngine(config, store)
            workflow = engine.ensure_workflow(datetime.fromisoformat("2026-05-25").date())
            engine._write_pending_approval(
                datetime.fromisoformat("2026-05-25").date(),
                "needs-human",
                "需要人工确认",
                {},
            )

            outbox_path = engine.write_attention_outbox(datetime.fromisoformat("2026-05-25").date(), workflow)
            tasks = load_outbox(outbox_path)

        self.assertEqual([task.conversation_name for task in tasks], ["OperatorUser"])
        self.assertEqual(tasks[0].kind, "ask_ares")

    def test_workflow_engine_builds_ops_table_at_18_with_missing_supplier_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.ops_table_cutoff_time = "18:00"
            store = Store(config.db_path)
            store.init()
            image_path = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#eeeeee").save(image_path)
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址"))
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T10:00:00"),
                    primary_image=str(image_path),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(image_path)),
                    confidence=0.9,
                )
            )
            workflow = initialize_daily_workflow(
                "2026-05-25",
                [Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址")],
                [],
                [ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR])],
            )
            advance_supplier_status(workflow, "SUP01", STATUS_SAMPLE_REQUESTED, sample_requested_at="2026-05-25T15:00:00")
            workflow.selection_path = str(config.data_dir / "reports" / "2026-05-25" / "selection.json")
            write_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json", workflow)
            report_dir = config.data_dir / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "SUP01-260524-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": str(image_path),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (report_dir / "selection.json").write_text(json.dumps([{"product_id": "SUP01-260524-01"}], ensure_ascii=False), encoding="utf-8")

            result = WorkflowEngine(config, store).run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T18:01:00"),
            )
            replies = json.loads((report_dir / "supplier_replies.json").read_text(encoding="utf-8"))
            ops_exists = (report_dir / "ops_selected_product_info.xlsx").exists()

        self.assertIn("ops_table_cutoff_with_missing_supplier_info", result.actions)
        self.assertEqual(replies["items"][0]["status"], "supplier_no_reply")
        self.assertTrue(ops_exists)

    def test_desktop_outbox_sent_tasks_advance_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.runtime_mode = "desktop"
            store = Store(config.db_path)
            store.init()
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [ContactRole("SUP01", "测试供应商", roles=[ROLE_SUPPLIER], main_categories=["上衣/T恤"], sample_address="地址")],
            )
            run_date = datetime.fromisoformat("2026-05-25T00:00:00").date()
            engine = WorkflowEngine(config, store)
            engine.run_once(run_date, use_ai_style=False)
            outbox_path = config.data_dir / "tasks" / "2026-05-25" / "desktop_outbox.json"
            self.assertEqual(len(pending_outbox_tasks(load_outbox(outbox_path))), 1)
            mark_outbox_sent(outbox_path, ["ask:2026-05-25:SUP01"], datetime.fromisoformat("2026-05-25T09:05:00"))
            result = engine.run_once(run_date, use_ai_style=False)
            workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")

        self.assertIn("sent_outbox_applied", result.actions)
        self.assertEqual(workflow.suppliers[0].status, STATUS_WAITING_IMAGES)
        self.assertEqual(workflow.suppliers[0].sent_at, "2026-05-25T09:05:00")

    def test_mark_outbox_sent_records_actual_conversation_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            from supplier_bot.desktop_outbox import DesktopOutboxTask, upsert_outbox_tasks

            path = Path(tmp) / "outbox.json"
            upsert_outbox_tasks(
                path,
                [
                    DesktopOutboxTask(
                        task_id="report:2026-05-25:SELECTOR_A",
                        kind="send_report",
                        conversation_name="选款人甲",
                        search_text="选款人甲",
                        message="报表",
                    )
                ],
            )
            mark_outbox_sent(
                path,
                ["report:2026-05-25:SELECTOR_A"],
                datetime.fromisoformat("2026-05-25T10:00:00"),
                metadata_by_task_id={"report:2026-05-25:SELECTOR_A": {"actual_conversation_title": "选款人甲"}},
            )
            [task] = load_outbox(path)

        self.assertEqual(task.metadata["actual_conversation_title"], "选款人甲")

    def test_desktop_sender_can_skip_supplier_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            from supplier_bot.desktop_outbox import DesktopOutboxTask, upsert_outbox_tasks

            path = Path(tmp) / "outbox.json"
            upsert_outbox_tasks(
                path,
                [
                    DesktopOutboxTask(
                        task_id="ask:2026-05-29:SUP01",
                        kind="ask_supplier",
                        conversation_name="供应商",
                        search_text="供应商",
                        message="问款",
                    ),
                    DesktopOutboxTask(
                        task_id="report:2026-05-29:SELECTOR_A",
                        kind="send_report",
                        conversation_name="选款人甲",
                        search_text="选款人甲",
                        message="报表",
                    ),
                ],
            )

            result = send_pending_desktop_outbox(path, limit=10, excluded_kinds=["ask_supplier"], dry_run=True)

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.skipped, 1)

    def test_server_sync_preserves_local_contact_roles_and_desktop_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            from scripts import run_daily_operator_agent as runner
            from supplier_bot.desktop_outbox import DesktopOutboxTask, write_outbox

            data_dir = Path(tmp) / "data"
            contacts_path = data_dir / "wecom_contacts.json"
            outbox_path = data_dir / "tasks" / "2026-06-03" / "desktop_outbox.json"
            write_contact_roles(
                contacts_path,
                [
                    ContactRole(
                        "SUP_LOCAL",
                        "本地供应商",
                        roles=[ROLE_SUPPLIER],
                        search_text="本地供应商",
                    )
                ],
            )
            write_outbox(
                outbox_path,
                [
                    DesktopOutboxTask(
                        task_id="ask:2026-06-03:SUP_LOCAL",
                        kind="ask_supplier",
                        conversation_name="本地供应商",
                        search_text="本地供应商",
                        message="本地问款",
                    )
                ],
            )

            def fake_rsync(command, **_kwargs):
                self.assertIn("--exclude=tasks/*/desktop_outbox.json", command)
                self.assertIn("--exclude=tasks/*/desktop_ask_batch_*.json", command)
                write_contact_roles(contacts_path, [])
                return Mock(stdout="", stderr="")

            old_data_dir = runner.config.data_dir
            old_target = runner.config.server_sync_target
            try:
                runner.config.data_dir = data_dir
                runner.config.server_sync_target = "example.test:/srv/supplier-bot"
                with patch.object(runner.subprocess, "run", fake_rsync), patch.object(runner, "append_log"):
                    runner.sync_server_data_to_local(datetime.fromisoformat("2026-06-03T00:00:00").date())
            finally:
                runner.config.data_dir = old_data_dir
                runner.config.server_sync_target = old_target

            contacts = load_contact_roles(contacts_path)
            outbox = load_outbox(outbox_path)

        self.assertEqual([(contact.contact_id, contact.display_name, contact.roles) for contact in contacts], [("SUP_LOCAL", "本地供应商", [ROLE_SUPPLIER])])
        self.assertEqual([task.task_id for task in outbox], ["ask:2026-06-03:SUP_LOCAL"])
        self.assertEqual(outbox[0].message, "本地问款")

    def test_server_sync_merges_remote_external_id_without_losing_local_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            from scripts import run_daily_operator_agent as runner

            data_dir = Path(tmp) / "data"
            contacts_path = data_dir / "wecom_contacts.json"
            write_contact_roles(
                contacts_path,
                [
                    ContactRole(
                        "SUP_LOCAL",
                        "本地供应商",
                        roles=[ROLE_SUPPLIER],
                        search_text="本地搜索名",
                        sample_address="本地地址",
                    )
                ],
            )

            def fake_rsync(_command, **_kwargs):
                write_contact_roles(
                    contacts_path,
                    [
                        ContactRole(
                            "wm_remote",
                            "本地供应商",
                            source="wecom_external_contact",
                            external_user_id="wm_remote",
                            roles=[],
                            search_text="服务器名称",
                        )
                    ],
                )
                return Mock(stdout="", stderr="")

            old_data_dir = runner.config.data_dir
            old_target = runner.config.server_sync_target
            try:
                runner.config.data_dir = data_dir
                runner.config.server_sync_target = "example.test:/srv/supplier-bot"
                with patch.object(runner.subprocess, "run", fake_rsync), patch.object(runner, "append_log"):
                    runner.sync_server_data_to_local(datetime.fromisoformat("2026-06-03T00:00:00").date())
            finally:
                runner.config.data_dir = old_data_dir
                runner.config.server_sync_target = old_target

            [contact] = load_contact_roles(contacts_path)

        self.assertEqual(contact.contact_id, "SUP_LOCAL")
        self.assertEqual(contact.external_user_id, "wm_remote")
        self.assertEqual(contact.roles, [ROLE_SUPPLIER])
        self.assertEqual(contact.search_text, "本地搜索名")
        self.assertEqual(contact.sample_address, "本地地址")

    def test_inbox_events_queue_and_ingest_supplier_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(data_dir / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "张姐", "wm_1", ["上衣/T恤"], "地址"))
            image = tmp_path / "incoming.jpg"
            Image.new("RGB", (800, 1000), "#dddddd").save(image)

            event_path = queue_inbox_event(
                data_dir,
                InboxEvent(
                    event_id="evt-1",
                    supplier_id="SUP01",
                    received_at="2026-05-25T10:30:00",
                    image_paths=[str(image)],
                    source="desktop_fallback",
                ),
            )
            result = process_pending_inbox_events(store, data_dir, root_dir=tmp_path)

            products = store.list_products_for_date("2026-05-25")

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        self.assertFalse(event_path.exists())
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].supplier_id, "SUP01")
        self.assertEqual(result.created_product_ids, [products[0].product_id])

    def test_supplier_images_after_report_finalize_go_to_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(data_dir / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "张姐", "wm_1", ["上衣/T恤"], "地址"))
            image = tmp_path / "late.jpg"
            Image.new("RGB", (800, 1000), "#dddddd").save(image)

            queue_inbox_event(
                data_dir,
                InboxEvent(
                    event_id="evt-late",
                    supplier_id="SUP01",
                    received_at="2026-05-25T15:05:00",
                    image_paths=[str(image)],
                    source="wecom_archive",
                ),
            )
            result = process_pending_inbox_events(
                store,
                data_dir,
                root_dir=tmp_path,
                report_finalize_time="15:00",
            )

            today_products = store.list_products_for_date("2026-05-25")
            next_day_products = store.list_products_for_date("2026-05-26")

        self.assertEqual(result.processed, 1)
        self.assertEqual(today_products, [])
        self.assertEqual(len(next_day_products), 1)
        self.assertTrue(next_day_products[0].product_id.startswith("SUP01-260526-"))

    def test_wecom_archive_poll_queues_supplier_image_once(self):
        class FakeArchiveAdapter:
            def __init__(self, image_bytes):
                self.image_bytes = image_bytes

            def get_chat_data(self, seq, limit):
                return [
                    {
                        "seq": 11,
                        "msgid": "msg-001",
                        "from": "wm_supplier",
                        "tolist": ["OperatorUser"],
                        "msgtype": "image",
                        "msgtime": int(datetime.fromisoformat("2026-05-25T10:00:00").timestamp() * 1000),
                        "image": {"sdkfileid": "sdk-file-1"},
                    }
                ]

            def download_media(self, sdkfileid):
                return self.image_bytes

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "张姐", "wm_supplier", ["上衣/T恤"], "测试地址"))
            buffer = BytesIO()
            Image.new("RGB", (640, 800), "#efefef").save(buffer, format="JPEG")
            test_config = Config()
            test_config.data_dir = data_dir

            result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(buffer.getvalue()),
            )
            process_result = process_pending_inbox_events(store, data_dir, root_dir=tmp_path)
            duplicate = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(buffer.getvalue()),
            )

            self.assertEqual(result.checked, 1)
            self.assertEqual(result.queued_events, 1)
            self.assertEqual(process_result.processed, 1)
            self.assertEqual(len(store.list_products()), 1)
            self.assertEqual(duplicate.queued_events, 0)

    def test_wecom_archive_does_not_advance_seq_when_media_download_fails(self):
        class FakeArchiveAdapter:
            def get_chat_data(self, seq, limit):
                return [
                    {
                        "seq": 11,
                        "msgid": "msg-broken-image",
                        "from": "wm_supplier",
                        "tolist": ["OperatorUser"],
                        "msgtype": "image",
                        "msgtime": int(datetime.fromisoformat("2026-05-25T10:00:00").timestamp() * 1000),
                        "image": {"sdkfileid": "sdk-file-broken"},
                    },
                    {
                        "seq": 12,
                        "msgid": "msg-later-text",
                        "from": "wm_supplier",
                        "tolist": ["OperatorUser"],
                        "msgtype": "text",
                        "msgtime": int(datetime.fromisoformat("2026-05-25T10:01:00").timestamp() * 1000),
                        "text": {"content": "后面这条不能让 seq 越过失败图片"},
                    },
                ]

            def download_media(self, sdkfileid):
                raise RuntimeError("download failed")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "张姐", "wm_supplier", ["上衣/T恤"], "测试地址"))
            test_config = Config()
            test_config.data_dir = data_dir
            test_config.wecom_msg_audit_start_seq = 10

            result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(),
            )
            state = json.loads((data_dir / "runtime" / "wecom_archive_state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.new_seq, 10)
        self.assertEqual(state["seq"], 10)
        self.assertTrue(result.errors)

    def test_wecom_archive_normalizes_text_and_second_timestamps(self):
        timestamp = int(datetime.fromisoformat("2026-05-25T10:01:00").timestamp())
        message = normalize_archive_message(
            {
                "seq": 12,
                "msgid": "msg-002",
                "from": "wm_supplier",
                "tolist": ["OperatorUser"],
                "msgtype": "text",
                "msgtime": timestamp,
                "text": {"content": "这款有现货"},
            }
        )

        self.assertEqual(message.text, "这款有现货")
        self.assertEqual(message.msgtime, parse_msgtime(timestamp))

    def test_wecom_archive_preserves_unknown_sender_images_for_mapping(self):
        class FakeArchiveAdapter:
            def get_chat_data(self, seq, limit):
                return [
                    {
                        "seq": 21,
                        "msgid": "unknown-msg",
                        "from": "wm_unknown",
                        "tolist": ["OperatorUser"],
                        "msgtype": "image",
                        "msgtime": int(datetime.fromisoformat("2026-05-25T11:00:00").timestamp() * 1000),
                        "image": {"sdkfileid": "sdk-file-unknown"},
                    }
                ]

            def download_media(self, sdkfileid):
                buffer = BytesIO()
                Image.new("RGB", (320, 320), "#d8d8d8").save(buffer, format="JPEG")
                return buffer.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            test_config = Config()
            test_config.data_dir = data_dir

            result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(),
            )

            self.assertEqual(result.checked, 1)
            self.assertEqual(result.queued_events, 0)
            self.assertEqual(result.unknown_events, 1)
            self.assertEqual(result.downloaded_media, 1)
            self.assertEqual(len(list((data_dir / "archive_unknown" / "2026-05-25").glob("*.json"))), 1)
            self.assertEqual(len(list((data_dir / "archive_media" / "2026-05-25").glob("UNKNOWN_*/*.jpg"))), 1)

    def test_wecom_archive_binds_unknown_supplier_text_then_routes_images(self):
        class FakeArchiveAdapter:
            def get_chat_data(self, seq, limit):
                base_time = int(datetime.fromisoformat("2026-05-25T11:00:00").timestamp() * 1000)
                return [
                    {
                        "seq": 21,
                        "msgid": "intro-msg",
                        "from": "wm_supplier_real",
                        "tolist": ["OperatorUser"],
                        "msgtype": "text",
                        "msgtime": base_time,
                        "text": {"content": "我是杭州初白服饰，今天有新款"},
                    },
                    {
                        "seq": 22,
                        "msgid": "image-msg",
                        "from": "wm_supplier_real",
                        "tolist": ["OperatorUser"],
                        "msgtype": "image",
                        "msgtime": base_time + 1000,
                        "image": {"sdkfileid": "sdk-file"},
                    },
                ]

            def download_media(self, sdkfileid):
                buffer = BytesIO()
                Image.new("RGB", (320, 320), "#d8d8d8").save(buffer, format="JPEG")
                return buffer.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            test_config = Config()
            test_config.data_dir = data_dir
            write_contact_roles(
                data_dir / "wecom_contacts.json",
                [ContactRole("SUP01", "杭州初白服饰", roles=[ROLE_SUPPLIER])],
            )

            result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(),
            )
            contacts = load_contact_roles(data_dir / "wecom_contacts.json")
            supplier = store.get_supplier("SUP01")
            pending_count = len(list((data_dir / "inbox_events" / "pending").glob("*.json")))
            media_count = len(list((data_dir / "archive_media" / "2026-05-25" / "SUP01").glob("*.jpg")))

        self.assertEqual(result.unknown_events, 0)
        self.assertEqual(result.queued_events, 2)
        self.assertEqual(contacts[0].external_user_id, "wm_supplier_real")
        self.assertEqual(supplier.external_user_id, "wm_supplier_real")
        self.assertEqual(pending_count, 2)
        self.assertEqual(media_count, 1)

    def test_wecom_archive_binds_unbound_supplier_from_sent_desktop_outbox_order(self):
        class FakeArchiveAdapter:
            def get_chat_data(self, seq, limit):
                base_time = int(datetime.fromisoformat("2026-05-25T09:00:00").timestamp() * 1000)
                return [
                    {
                        "seq": 21,
                        "msgid": "out-a",
                        "from": "internal_user",
                        "tolist": ["wm_supplier_a"],
                        "msgtype": "text",
                        "msgtime": base_time,
                        "text": {"content": "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。"},
                    },
                    {
                        "seq": 22,
                        "msgid": "out-b",
                        "from": "internal_user",
                        "tolist": ["wm_supplier_b"],
                        "msgtype": "text",
                        "msgtime": base_time + 1000,
                        "text": {"content": "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。"},
                    },
                    {
                        "seq": 23,
                        "msgid": "image-b",
                        "from": "wm_supplier_b",
                        "tolist": ["internal_user"],
                        "msgtype": "image",
                        "msgtime": base_time + 2000,
                        "image": {"sdkfileid": "sdk-file"},
                    },
                ]

            def download_media(self, sdkfileid):
                buffer = BytesIO()
                Image.new("RGB", (320, 320), "#d8d8d8").save(buffer, format="JPEG")
                return buffer.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            tasks_dir = data_dir / "tasks" / "2026-05-25"
            tasks_dir.mkdir(parents=True)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            test_config = Config()
            test_config.data_dir = data_dir
            write_contact_roles(
                data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP_A", "供应商A", roles=[ROLE_SUPPLIER]),
                    ContactRole("SUP_B", "供应商B", roles=[ROLE_SUPPLIER]),
                ],
            )
            (tasks_dir / "desktop_outbox.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {
                                "task_id": "ask:2026-05-25:SUP_A",
                                "kind": "ask_supplier",
                                "conversation_name": "供应商A",
                                "search_text": "供应商A",
                                "message": "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。",
                                "status": "sent",
                                "metadata": {"supplier_id": "SUP_A"},
                            },
                            {
                                "task_id": "ask:2026-05-25:SUP_B",
                                "kind": "ask_supplier",
                                "conversation_name": "供应商B",
                                "search_text": "供应商B",
                                "message": "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。",
                                "status": "sent",
                                "metadata": {"supplier_id": "SUP_B"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(),
            )
            contacts = {contact.contact_id: contact for contact in load_contact_roles(data_dir / "wecom_contacts.json")}
            pending = list((data_dir / "inbox_events" / "pending").glob("*.json"))
            pending_payload = json.loads(pending[0].read_text()) if pending else {}

        self.assertEqual(contacts["SUP_A"].external_user_id, "wm_supplier_a")
        self.assertEqual(contacts["SUP_B"].external_user_id, "wm_supplier_b")
        self.assertEqual(result.queued_events, 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending_payload["supplier_id"], "SUP_B")

    def test_wecom_archive_binds_from_historical_unknown_outbox_and_recovers_images(self):
        class FakeArchiveAdapter:
            def get_chat_data(self, seq, limit):
                return []

            def download_media(self, sdkfileid):
                return b""

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            tasks_dir = data_dir / "tasks" / "2026-06-02"
            unknown_dir = data_dir / "archive_unknown" / "2026-06-02"
            media_dir = data_dir / "archive_media" / "2026-06-02" / "UNKNOWN_wm_supplier_a_real"
            tasks_dir.mkdir(parents=True)
            unknown_dir.mkdir(parents=True)
            media_dir.mkdir(parents=True)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            test_config = Config()
            test_config.data_dir = data_dir
            write_contact_roles(
                data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP_DEMO_A", "示例供应商甲", roles=[ROLE_SUPPLIER]),
                    ContactRole("SUP_DEMO_B", "示例供应商乙", external_user_id="wm_supplier_b", roles=[ROLE_SUPPLIER]),
                ],
            )
            question = "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。"
            (tasks_dir / "desktop_outbox.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {
                                "task_id": "ask:2026-06-02:SUP_DEMO_A",
                                "kind": "ask_supplier",
                                "conversation_name": "示例供应商甲",
                                "search_text": "示例供应商甲",
                                "message": question,
                                "status": "sent",
                                "created_at": "2026-06-02T11:47:00",
                                "sent_at": "2026-06-02T11:47:28",
                                "metadata": {"supplier_id": "SUP_DEMO_A"},
                            },
                            {
                                "task_id": "ask:2026-06-02:SUP_DEMO_B",
                                "kind": "ask_supplier",
                                "conversation_name": "示例供应商乙",
                                "search_text": "示例供应商乙",
                                "message": question,
                                "status": "sent",
                                "created_at": "2026-06-02T11:47:01",
                                "sent_at": "2026-06-02T11:47:28",
                                "metadata": {"supplier_id": "SUP_DEMO_B"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (unknown_dir / "out_supplier_a.json").write_text(
                json.dumps(
                    {
                        "seq": 124,
                        "msgid": "out_supplier_a",
                        "sender": "internal_user",
                        "recipients": ["wm_supplier_a_real"],
                        "msgtype": "text",
                        "msgtime": "2026-06-02T11:47:18",
                        "text": question,
                        "image_paths": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (unknown_dir / "out_supplier_b.json").write_text(
                json.dumps(
                    {
                        "seq": 125,
                        "msgid": "out_supplier_b",
                        "sender": "internal_user",
                        "recipients": ["wm_supplier_b"],
                        "msgtype": "text",
                        "msgtime": "2026-06-02T11:47:25",
                        "text": question,
                        "image_paths": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            image_path = media_dir / "image_supplier_a.jpg"
            image_path.write_bytes(b"fake-image")
            (unknown_dir / "image_supplier_a.json").write_text(
                json.dumps(
                    {
                        "seq": 126,
                        "msgid": "image_supplier_a",
                        "sender": "wm_supplier_a_real",
                        "recipients": ["internal_user"],
                        "msgtype": "image",
                        "msgtime": "2026-06-02T11:48:39",
                        "text": "",
                        "image_paths": [str(image_path)],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-06-02",
                adapter=FakeArchiveAdapter(),
            )
            contacts = {contact.contact_id: contact for contact in load_contact_roles(data_dir / "wecom_contacts.json")}
            pending = list((data_dir / "inbox_events" / "pending").glob("*.json"))
            pending_payload = json.loads(pending[0].read_text(encoding="utf-8")) if pending else {}
            unresolved = list(unknown_dir.glob("*.json"))
            resolved = list((data_dir / "archive_unknown_resolved" / "2026-06-02").glob("*.json"))

        self.assertEqual(contacts["SUP_DEMO_A"].external_user_id, "wm_supplier_a_real")
        self.assertEqual(result.queued_events, 1)
        self.assertEqual(pending_payload["supplier_id"], "SUP_DEMO_A")
        self.assertEqual(len(unresolved), 0)
        self.assertEqual(len(resolved), 3)

    def test_recover_bound_unknown_archive_events_queues_existing_unknown_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            unknown_dir = data_dir / "archive_unknown" / "2026-05-25"
            media_dir = data_dir / "archive_media" / "2026-05-25" / "UNKNOWN_wm_supplier"
            unknown_dir.mkdir(parents=True)
            media_dir.mkdir(parents=True)
            image_path = media_dir / "msg.jpg"
            Image.new("RGB", (320, 320), "#d8d8d8").save(image_path)
            (unknown_dir / "msg.json").write_text(
                json.dumps(
                    {
                        "msgid": "msg",
                        "sender": "wm_supplier",
                        "msgtype": "image",
                        "msgtime": "2026-05-25T10:00:00",
                        "image_paths": [str(image_path)],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contacts = [ContactRole("SUP01", "供应商", external_user_id="wm_supplier", roles=[ROLE_SUPPLIER])]
            suppliers = contacts_to_suppliers(contacts)

            recovered = recover_bound_unknown_archive_events(data_dir, contacts, suppliers)
            recovered_again = recover_bound_unknown_archive_events(data_dir, contacts, suppliers)
            pending = list((data_dir / "inbox_events" / "pending").glob("*.json"))
            pending_payload = json.loads(pending[0].read_text()) if pending else {}

        self.assertEqual(recovered, 1)
        self.assertEqual(recovered_again, 0)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending_payload["supplier_id"], "SUP01")

    def test_wecom_archive_routes_selector_screenshot_to_selection(self):
        class FakeArchiveAdapter:
            def __init__(self, image_bytes):
                self.image_bytes = image_bytes

            def get_chat_data(self, seq, limit):
                return [
                    {
                        "seq": 31,
                        "msgid": "selector-msg",
                        "from": "wm_selector",
                        "tolist": ["OperatorUser"],
                        "msgtype": "image",
                        "msgtime": int(datetime.fromisoformat("2026-05-25T15:00:00").timestamp() * 1000),
                        "image": {"sdkfileid": "sdk-selector"},
                    }
                ]

            def download_media(self, sdkfileid):
                return self.image_bytes

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            write_contact_roles(
                data_dir / "wecom_contacts.json",
                [ContactRole("selector_1", "选款人甲", external_user_id="wm_selector", roles=[ROLE_SELECTOR])],
            )
            workflow = initialize_daily_workflow(
                "2026-05-25",
                [],
                [ContactRole("selector_1", "选款人甲", external_user_id="wm_selector", roles=[ROLE_SELECTOR])],
                [],
            )
            report_dir = data_dir / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            workflow.report_path = str(report_dir / "report.png")
            write_daily_workflow(data_dir / "tasks" / "2026-05-25" / "daily_workflow.json", workflow)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "page_width": 600,
                        "products": [
                            {
                                "product_id": "SUP01-260524-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": "missing.jpg",
                                "box": [80, 80, 260, 260],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            marked = Image.new("RGB", (600, 400), "white")
            from PIL import ImageDraw

            ImageDraw.Draw(marked).rectangle((70, 70, 270, 270), outline="#1677ff", width=18)
            buffer = BytesIO()
            marked.save(buffer, format="JPEG")
            test_config = Config()
            test_config.data_dir = data_dir

            archive_result = poll_message_archive_into_inbox(
                test_config,
                store,
                data_dir,
                "2026-05-25",
                adapter=FakeArchiveAdapter(buffer.getvalue()),
            )
            inbox_result = process_pending_inbox_events(store, data_dir, root_dir=tmp_path)
            selection = json.loads((report_dir / "selection.json").read_text(encoding="utf-8"))

        self.assertEqual(archive_result.queued_events, 1)
        self.assertEqual(inbox_result.reply_product_ids, ["SUP01-260524-01"])
        self.assertEqual(selection[0]["product_id"], "SUP01-260524-01")
        self.assertIn("screenshot_path", selection[0])

    def test_desktop_receiver_extracts_incoming_image_crops(self):
        canvas = Image.new("RGB", (960, 640), "#ffffff")
        canvas.paste("#245fb6", (330, 90, 410, 230))
        canvas.paste("#2c6b3d", (330, 260, 420, 420))
        crops = extract_incoming_image_crops(canvas)

        self.assertEqual(len(crops), 2)
        self.assertTrue(all(crop.width >= 70 and crop.height >= 130 for crop in crops))

    def test_capture_selector_selection_writes_selection_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            report_dir = data_dir / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            workflow = initialize_daily_workflow(
                "2026-05-25",
                [Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址")],
                [ContactRole("SELECTOR_A", "选款人甲", roles=[ROLE_SELECTOR], search_text="选款人甲")],
                [],
            )
            advance_supplier_status(workflow, "SUP01", STATUS_REPORT_SENT)
            write_daily_workflow(data_dir / "tasks" / "2026-05-25" / "daily_workflow.json", workflow)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "page_width": 600,
                        "products": [
                            {
                                "product_id": "SUP01-260524-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": "images/SUP01-260524-01.jpg",
                                "box": [80, 80, 320, 360],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            screenshot = tmp_path / "selector_mark.jpg"
            marked = Image.new("RGB", (600, 480), "white")
            from PIL import ImageDraw

            ImageDraw.Draw(marked).rectangle((70, 70, 330, 370), outline="#e31937", width=18)
            marked.save(screenshot)
            capture = DesktopCapture("SELECTOR_A", "选款人甲", [str(screenshot)], pages_scanned=4, stop_reason="found_daily_ask_message")

            with patch("supplier_bot.desktop_receiver.check_desktop_automation", return_value=Mock(ok=True)), patch(
                "supplier_bot.desktop_receiver.check_desktop_capture", return_value=Mock(ok=True)
            ), patch(
                "supplier_bot.desktop_receiver.capture_conversation_image_crops",
                return_value=capture,
            ) as mock_capture:
                result = capture_selector_selections(data_dir, datetime.fromisoformat("2026-05-25").date())

            selection_payload = json.loads((report_dir / "selection.json").read_text(encoding="utf-8"))

        self.assertEqual(result.selection_products, ["SUP01-260524-01"])
        self.assertEqual(selection_payload[0]["selector_name"], "选款人甲")
        self.assertEqual(selection_payload[0]["capture_mode"], "after_report_message_boundary")
        self.assertEqual(selection_payload[0]["capture_pages_scanned"], 4)
        self.assertEqual(mock_capture.call_args.kwargs["scan_pages"], 12)
        self.assertEqual(mock_capture.call_args.kwargs["stop_text"], "这是今天的选款报表")

    def test_workflow_waits_when_supplier_reply_is_only_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            store = Store(config.db_path)
            store.init()
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址"))
            image_dir = tmp_path / "images"
            image_dir.mkdir()
            product_image = image_dir / "tee.jpg"
            Image.new("RGB", (800, 1000), "#eeeeee").save(product_image)
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T10:00:00"),
                    primary_image=str(product_image),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(product_image)),
                    confidence=0.9,
                )
            )
            workflow = initialize_daily_workflow(
                "2026-05-25",
                [Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址")],
                [],
                [ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR])],
            )
            advance_supplier_status(workflow, "SUP01", STATUS_SAMPLE_REQUESTED, sample_requested_at="2026-05-25T10:10:00")
            write_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json", workflow)
            report_dir = config.data_dir / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "SUP01-260524-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": str(product_image),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (report_dir / "selection.json").write_text(
                json.dumps([{"product_id": "SUP01-260524-01"}], ensure_ascii=False),
                encoding="utf-8",
            )
            (report_dir / "supplier_replies.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "product_id": "SUP01-260524-01",
                                "status": "waiting_product_info",
                                "style_no": "",
                                "color": "",
                                "sizes": "",
                                "material": "",
                                "price": "",
                                "stock_or_moq": "",
                                "lead_time": "",
                                "raw_reply": "稍后补资料",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = WorkflowEngine(config, store).run_once(
                datetime.fromisoformat("2026-05-25").date(),
                use_ai_style=False,
                now=datetime.fromisoformat("2026-05-25T17:00:00"),
            )
            workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")
            ops_exists = (report_dir / "ops_selected_product_info.xlsx").exists()

        self.assertIn("ops_table_waiting_for_supplier_info", result.actions)
        self.assertEqual(workflow.suppliers[0].status, STATUS_SAMPLE_REQUESTED)
        self.assertFalse(ops_exists)

    def test_official_archive_text_reply_builds_supplier_replies_and_ops_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            store = Store(config.db_path)
            store.init()
            store.upsert_supplier(Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址"))
            image_dir = tmp_path / "images"
            image_dir.mkdir()
            product_image = image_dir / "tee.jpg"
            Image.new("RGB", (800, 1000), "#eeeeee").save(product_image)
            store.upsert_product(
                Product(
                    product_id="SUP01-260524-01",
                    supplier_id="SUP01",
                    received_at=datetime.fromisoformat("2026-05-25T10:00:00"),
                    primary_image=str(product_image),
                    related_images=[],
                    category_lv1="上衣",
                    category_lv2="T恤",
                    phash=phash(str(product_image)),
                    confidence=0.9,
                )
            )
            workflow = initialize_daily_workflow(
                "2026-05-25",
                [Supplier("SUP01", "测试供应商", "测试供应商", "wm_1", ["上衣/T恤"], "地址")],
                [],
                [ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR])],
            )
            advance_supplier_status(workflow, "SUP01", STATUS_SAMPLE_REQUESTED, sample_requested_at="2026-05-25T10:10:00")
            write_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json", workflow)
            report_dir = config.data_dir / "reports" / "2026-05-25"
            report_dir.mkdir(parents=True)
            (report_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "product_id": "SUP01-260524-01",
                                "supplier_id": "SUP01",
                                "supplier_name": "测试供应商",
                                "category": "上衣/T恤",
                                "primary_image": str(product_image),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (report_dir / "selection.json").write_text(
                json.dumps([{"product_id": "SUP01-260524-01"}], ensure_ascii=False),
                encoding="utf-8",
            )
            queue_inbox_event(
                config.data_dir,
                InboxEvent(
                    event_id="archive-text-1",
                    supplier_id="SUP01",
                    received_at="2026-05-25T10:30:00",
                    text="SUP01-260524-01 货号：T26052401 颜色：米白 尺码：S/M/L 面料：棉 价格：99 库存：现货 发货：明天",
                    source="wecom_archive",
                ),
            )

            inbox_result = process_pending_inbox_events(store, config.data_dir, root_dir=tmp_path)
            workflow_result = WorkflowEngine(config, store).run_once(datetime.fromisoformat("2026-05-25").date(), use_ai_style=False)
            workflow = load_daily_workflow(config.data_dir / "tasks" / "2026-05-25" / "daily_workflow.json")
            replies = json.loads((report_dir / "supplier_replies.json").read_text(encoding="utf-8"))
            ops_exists = (report_dir / "ops_selected_product_info.xlsx").exists()

        self.assertEqual(inbox_result.reply_product_ids, ["SUP01-260524-01"])
        self.assertEqual(replies["items"][0]["style_no"], "T26052401")
        self.assertIn("ops_table", " ".join(workflow_result.actions))
        self.assertEqual(workflow.suppliers[0].status, STATUS_INFO_RECEIVED)
        self.assertTrue(ops_exists)

    def test_health_checks_report_missing_and_configured_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.wecom_corp_id = "corp"
            config.wecom_agent_secret = "secret"
            config.wecom_agent_id = "1000002"
            config.wecom_msg_audit_secret = "audit"
            config.wecom_msg_audit_private_key_path = "/tmp/key.pem"
            sdk_path = tmp_path / "libWeWorkFinanceSdk_C.so"
            sdk_path.write_bytes(b"fake-sdk")
            config.wecom_msg_audit_sdk_lib = str(sdk_path)
            config.runtime_mode = "official"
            config.data_dir.mkdir(parents=True)
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "测试供应商", external_user_id="wm_supplier", roles=[ROLE_SUPPLIER]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            script = tmp_path / "start_supplier_bot.command"
            script.write_text("#!/bin/zsh\n", encoding="utf-8")
            script.chmod(0o755)

            payload = health_payload(run_health_checks(config, project_root=tmp_path))

        self.assertTrue(payload["ok"])
        by_name = {item["name"]: item for item in payload["checks"]}
        self.assertTrue(by_name["official_api"]["ok"])
        self.assertTrue(by_name["message_archive"]["ok"])
        self.assertIn("1 suppliers", by_name["supplier_roles"]["detail"])
        self.assertIn("测试供应商", by_name["supplier_roles"]["detail"])
        self.assertTrue(by_name["desktop_title_guard"]["ok"])

    def test_health_checks_fail_when_supplier_missing_external_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.runtime_mode = "official"
            config.data_dir.mkdir(parents=True)
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "未绑定供应商", roles=[ROLE_SUPPLIER]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            script = tmp_path / "start_supplier_bot.command"
            script.write_text("#!/bin/zsh\n", encoding="utf-8")
            script.chmod(0o755)

            payload = health_payload(run_health_checks(config, project_root=tmp_path))

        self.assertFalse(payload["ok"])
        by_name = {item["name"]: item for item in payload["checks"]}
        self.assertFalse(by_name["supplier_external_ids"]["ok"])
        self.assertIn("未绑定供应商", by_name["supplier_external_ids"]["detail"])

    def test_health_checks_fail_official_mode_when_archive_sdk_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config()
            config.data_dir = tmp_path / "data"
            config.db_path = config.data_dir / "bot.sqlite3"
            config.wecom_corp_id = "corp"
            config.wecom_agent_secret = "secret"
            config.wecom_msg_audit_secret = "audit"
            config.wecom_msg_audit_private_key_path = "/tmp/key.pem"
            config.wecom_msg_audit_sdk_lib = ""
            config.runtime_mode = "official"
            config.data_dir.mkdir(parents=True)
            write_contact_roles(
                config.data_dir / "wecom_contacts.json",
                [
                    ContactRole("SUP01", "测试供应商", external_user_id="wm_supplier", roles=[ROLE_SUPPLIER]),
                    ContactRole("selector_1", "选款人甲", roles=[ROLE_SELECTOR]),
                    ContactRole("operator_1", "OperatorUser", roles=[ROLE_OPERATOR]),
                ],
            )
            script = tmp_path / "start_supplier_bot.command"
            script.write_text("#!/bin/zsh\n", encoding="utf-8")
            script.chmod(0o755)

            payload = health_payload(run_health_checks(config, project_root=tmp_path))

        self.assertFalse(payload["ok"])
        by_name = {item["name"]: item for item in payload["checks"]}
        self.assertFalse(by_name["message_archive"]["ok"])
        self.assertFalse(by_name["message_archive_sdk"]["ok"])

    def test_build_douyin_listing_drafts_and_exports_review_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "杭州初白服饰", "张姐", "external_1", ["上衣/T恤"], "测试地址"))

            image = tmp_path / "tee.jpg"
            Image.new("RGB", (800, 1000), "#eeeeee").save(image)
            products = ingest_supplier_images(store, tmp_path, "S01", [image], datetime.fromisoformat("2026-05-22T10:00:00"))
            selection = tmp_path / "selection.json"
            selection.write_text(json.dumps([{"product_id": products[0].product_id}], ensure_ascii=False), encoding="utf-8")

            product_ids = load_selection_product_ids(selection)
            selected_products = choose_products(store, None, product_ids)
            defaults = ListingDefaults(
                default_price_cents=12900,
                default_stock=8,
                freight_template_id="FT001",
                category_ids={"上衣/T恤": "CAT001"},
            )
            drafts = build_listing_drafts(store, selected_products, defaults)
            json_path, csv_path = write_listing_outputs(drafts, tmp_path / "listings")
            mark_drafted_products(store, drafts)

            self.assertEqual(len(drafts), 1)
            self.assertTrue(drafts[0].ready_to_publish)
            self.assertEqual(drafts[0].douyin_category_id, "CAT001")
            self.assertEqual([sku.size for sku in drafts[0].skus], ["S", "M", "L"])
            self.assertEqual(drafts[0].skus[0].price_cents, 12900)
            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertIn("已生成上架草稿", store.get_product(products[0].product_id).status.value)

    def test_douyin_listing_draft_requires_platform_fields_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = Store(tmp_path / "bot.sqlite3")
            store.init()
            store.upsert_supplier(Supplier("S01", "杭州初白服饰", "张姐", "external_1", ["上衣/T恤"], "测试地址"))
            missing_image_product = Product(
                product_id="S01-260522-01",
                supplier_id="S01",
                received_at=datetime.fromisoformat("2026-05-22T10:00:00"),
                primary_image=str(tmp_path / "missing.jpg"),
                related_images=[],
                category_lv1="上衣",
                category_lv2="T恤",
                phash="0" * 16,
            )
            store.upsert_product(missing_image_product)

            drafts = build_listing_drafts(store, [missing_image_product], ListingDefaults())

            self.assertFalse(drafts[0].ready_to_publish)
            self.assertIn("抖店类目ID", drafts[0].missing_fields)
            self.assertIn("运费模板ID", drafts[0].missing_fields)
            self.assertTrue(drafts[0].warnings[0].startswith("图片不存在"))

    def test_csv_listing_rows_and_drafts_from_may_sheet_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "may.csv"
            csv_path.write_text(
                "\ufeff上架时间,商品名字,供应商/工厂,工厂编码,产品图,线上编码,成本,售价,库存,尺码表,面料成分,产品卖点,店铺,类目,备注,商品名字(线上)\n"
                ",蓝调夜曲,奢纹,131,,KZ26050401,175,399,S89 M104,,60%亚麻 40%棉,,Lets xue 1,,,XUE【蓝调夜曲】 亚麻棉交织气球裤条纹休闲裤\n"
                ",圣洁国度,OUHER,LYQBV7911,,LYQ26051304,189,469,S M L,,100%棉,,Lets xue 1,,,\n"
                ",,,,,,,,,,,,,,,\n",
                encoding="utf-8",
            )
            image_dir = tmp_path / "images"
            image_dir.mkdir()
            (image_dir / "KZ26050401-main.jpg").write_bytes(b"fake")
            defaults = ListingDefaults(
                freight_template_id="FT001",
                category_ids={"下装/裤子": "CAT-PANTS", "连体/连衣裙": "CAT-DRESS"},
            )

            rows = load_csv_listing_rows(csv_path)
            drafts = build_listing_drafts_from_csv(csv_path, image_dir, defaults, product_name="蓝调夜曲")

            self.assertEqual(len(rows), 2)
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].external_code, "KZ26050401")
            self.assertEqual(drafts[0].images, [str(image_dir / "KZ26050401-main.jpg")])
            self.assertEqual([(sku.size, sku.stock) for sku in drafts[0].skus], [("S", 89), ("M", 104)])
            self.assertEqual(drafts[0].douyin_category_id, "CAT-PANTS")

            missing_title = build_listing_drafts_from_csv(csv_path, image_dir, defaults, external_code="LYQ26051304")
            self.assertIn("商品名字(线上)为空", "；".join(missing_title[0].warnings))

    def test_stock_parser_handles_common_inventory_texts(self):
        defaults = ListingDefaults(default_stock=7)

        skus, warnings = parse_stock_skus("S89 M104", "下装", 39900, defaults)
        self.assertEqual([(sku.color, sku.size, sku.stock) for sku in skus], [("默认色", "S", 89), ("默认色", "M", 104)])
        self.assertEqual(warnings, [])

        skus, warnings = parse_stock_skus("35-40", "鞋履", 59900, defaults)
        self.assertEqual([sku.size for sku in skus], ["35", "36", "37", "38", "39", "40"])
        self.assertEqual([sku.stock for sku in skus], [7, 7, 7, 7, 7, 7])
        self.assertIn("默认库存", warnings[0])

        skus, warnings = parse_stock_skus("S M L", "上衣", 19900, defaults)
        self.assertEqual([sku.size for sku in skus], ["S", "M", "L"])
        self.assertIn("未给数量", warnings[0])

        skus, warnings = parse_stock_skus("黑色S18M16 卡其色S3M19", "上衣", 49900, defaults)
        self.assertEqual(
            [(sku.color, sku.size, sku.stock) for sku in skus],
            [("黑色", "S", 18), ("黑色", "M", 16), ("卡其色", "S", 3), ("卡其色", "M", 19)],
        )
        self.assertEqual(warnings, [])

    def test_csv_image_matching_priority_and_category_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            images = [
                tmp_path / "KZ26050401-main.jpg",
                tmp_path / "KZ26050401-detail.png",
                tmp_path / "131-side.jpg",
                tmp_path / "蓝调夜曲.webp",
            ]
            for image in images:
                image.write_bytes(b"fake")
            product_dir = tmp_path / "仙本那"
            product_dir.mkdir()
            product_image = product_dir / "random-name.png"
            product_image.write_bytes(b"fake")

            matches, warnings = match_listing_images(images, "KZ26050401", "131", "蓝调夜曲")
            self.assertEqual([path.name for path in matches], ["KZ26050401-detail.png", "KZ26050401-main.jpg"])
            self.assertEqual(warnings, [])

            matches, warnings = match_listing_images(images, "", "131", "蓝调夜曲")
            self.assertEqual([path.name for path in matches], ["131-side.jpg"])
            self.assertIn("工厂编码", warnings[0])

            matches, warnings = match_listing_images([product_image], "SY26050406", "7716", "仙本那")
            self.assertEqual(matches, [product_image])
            self.assertIn("商品名字", warnings[0])

        self.assertEqual(infer_category("XUE【迷失麋鹿】 牛皮红色分趾鞋渐变喷漆百搭时尚平底单鞋")[:2], ("鞋履", "单鞋"))
        self.assertEqual(infer_category("XUE【蓝调夜曲】 亚麻棉交织气球裤条纹休闲裤")[:2], ("下装", "裤子"))


if __name__ == "__main__":
    unittest.main()
