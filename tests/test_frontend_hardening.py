from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendHardeningTests(unittest.TestCase):
    def test_channel_test_panel_is_bound_to_backend_and_preserves_range(self):
        source = (ROOT / "static/js/pages/channels.js").read_text(encoding="utf-8")
        self.assertIn("function channelTestPanel", source)
        self.assertIn("/channels/${channel.id}/test", source)
        self.assertIn("initialFilters = {}", source)
        self.assertIn("channels/overview?${qs({ range })}", source)
        self.assertNotIn('range=7d"', source)
        self.assertIn("按最近实际请求统计", source)
        self.assertIn("最多尝试", source)

    def test_shared_modal_and_toast_contracts_are_explicit(self):
        ui = (ROOT / "static/js/core/ui.js").read_text(encoding="utf-8")
        styles = (ROOT / "static/style.css").read_text(encoding="utf-8")
        self.assertIn("autofocus", ui)
        self.assertIn("document.documentElement.classList.add(\"modal-open\")", ui)
        self.assertIn("toastTypes", ui)
        self.assertIn("max-height: calc(100vh - 48px)", styles)
        self.assertIn("overflow-y: auto", styles)
        self.assertIn("left: 50%", styles)

    def test_route_and_chart_cleanup_hooks_exist(self):
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        charts = (ROOT / "static/js/core/charts.js").read_text(encoding="utf-8")
        logs = (ROOT / "static/js/pages/logs.js").read_text(encoding="utf-8")
        self.assertIn("state.routeVersion", app)
        self.assertIn("content.scrollTop = 0", app)
        self.assertIn("observer.disconnect()", charts)
        self.assertIn("labelStep", charts)
        self.assertIn("loadVersion", logs)

    def test_integration_uses_selected_protocol_model_key_and_structure_validation(self):
        source = (ROOT / "static/js/pages/integration.js").read_text(encoding="utf-8")
        self.assertIn("选择已上架模型", source)
        self.assertIn("选择有效网关 Key", source)
        self.assertIn("validateProtocolResponse", source)
        self.assertIn("client.responses.create", source)
        self.assertNotIn("const nativeFetch", source)
        self.assertNotIn("response.ok ?", source)

    def test_console_has_single_top_navigation_and_persisted_theme_control(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static/style.css").read_text(encoding="utf-8")
        self.assertIn('id="theme-select"', html)
        self.assertIn('data-nav="integration"', app)
        self.assertIn("localStorage", app)
        self.assertIn("data-theme=\"dark\"", styles)

    def test_model_channel_counts_use_public_model_names(self):
        source = (ROOT / "static/js/pages/models.js").read_text(encoding="utf-8")
        self.assertIn('models.map((model) => [model.model_name, 0])', source)

    def test_console_distinguishes_client_requests_attempts_and_real_bindings(self):
        overview = (ROOT / "static/js/pages/overview.js").read_text(encoding="utf-8")
        channels = (ROOT / "static/js/pages/channels.js").read_text(encoding="utf-8")
        models = (ROOT / "static/js/pages/models.js").read_text(encoding="utf-8")
        logs = (ROOT / "static/js/pages/logs.js").read_text(encoding="utf-8")
        self.assertIn("today_attempts", overview)
        self.assertIn("today_retries", overview)
        self.assertIn("上游尝试", channels)
        self.assertIn("available_channel_count", models)
        self.assertIn("upstream_model", models)
        self.assertIn("client_requests", logs)
        self.assertIn("failed_attempts", logs)
        self.assertIn("state.items.some((item) => item.id === state.selected)", logs)


    def test_console_surfaces_recovery_key_limits_proxy_cache_and_percentiles(self):
        channels = (ROOT / "static/js/pages/channels.js").read_text(encoding="utf-8")
        keys = (ROOT / "static/js/pages/keys.js").read_text(encoding="utf-8")
        settings = (ROOT / "static/js/pages/settings.js").read_text(encoding="utf-8")
        overview = (ROOT / "static/js/pages/overview.js").read_text(encoding="utf-8")
        self.assertIn("/recover", channels)
        self.assertIn("已自动禁用", channels)
        self.assertIn("f-proxy", channels)
        self.assertIn("allowed_models", keys)
        self.assertIn("rpm_limit", keys)
        self.assertIn("concurrency_limit", keys)
        self.assertIn("auto_disable_after", settings)
        self.assertIn("auto_recover_seconds", settings)
        self.assertIn("cache_enabled", settings)
        self.assertIn("p95_latency_ms", overview)
        self.assertIn("model_health", overview)
        self.assertIn("auto_disabled_channels", overview)
        self.assertIn("f-rpm", channels)
        self.assertIn("rpm_limit", channels)
        self.assertIn("retry_backoff_jitter_pct", settings)
        self.assertIn("retry_backoff_max_ms", settings)
        self.assertIn("models_require_key", settings)

    def test_console_surfaces_embeddings_and_channel_batch_actions(self):
        integration = (ROOT / "static/js/pages/integration.js").read_text(encoding="utf-8")
        channels = (ROOT / "static/js/pages/channels.js").read_text(encoding="utf-8")
        self.assertIn("embeddingsExample", integration)
        self.assertIn("/embeddings", integration)
        self.assertIn("unsupported_protocol", integration)
        self.assertIn('id="channel-batch-probe"', channels)
        self.assertIn('id="channel-batch-recover"', channels)
        self.assertIn("/channels/batch", channels)
        self.assertIn("data-channel-select", channels)


if __name__ == "__main__":
    unittest.main()
