"""Smoke tests estáticos: verificam que o redesign 'Aconchego em papel'
(fraunces/karla, papel quente, verde musgo) está presente nos templates.

São testes de presença de tokens — não há UI test framework no projeto.
"""
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BASE = (BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
DASH = (BASE_DIR / "templates" / "dashboard.html").read_text(encoding="utf-8")


class TestFontes(unittest.TestCase):
    def test_google_fonts_fraunces_karla(self):
        self.assertIn("family=Fraunces", BASE)

    def test_font_display_fraunces(self):
        self.assertIn("--font-display: 'Fraunces'", BASE)

    def test_font_body_karla(self):
        self.assertIn("--font-body: 'Karla'", BASE)

    def test_chart_font_fraunces(self):
        self.assertIn("'Fraunces', Georgia, serif", DASH)


class TestTemaClaroPapelQuente(unittest.TestCase):
    def test_bg_base_papel(self):
        self.assertIn("--bg-base: #f7f4ee", BASE)

    def test_bg_surface_papel(self):
        self.assertIn("--bg-surface: #fffdf7", BASE)

    def test_accent_verde_musgo(self):
        self.assertIn("--accent: #2f6f5e", BASE)

    def test_danger_terracota(self):
        self.assertIn("--danger: #b0705a", BASE)

    def test_warning_ocre(self):
        self.assertIn("--warning: #a5822f", BASE)

    def test_text_tinta(self):
        self.assertIn("--text-primary: #2b2620", BASE)
        self.assertIn("--text-secondary: #5c574e", BASE)

    def test_border_tinta_8porcento(self):
        self.assertIn("--border: rgba(43, 38, 32, 0.08)", BASE)

    def test_shadow_tinta_diluida(self):
        self.assertIn("rgba(43, 38, 32, 0.05)", BASE)

    def test_card_fechamento_musgo(self):
        self.assertIn("rgba(47, 111, 94, 0.12) 0%, #fffdf7 75%", BASE)

    def test_meta_theme_color_light_papel(self):
        # Ambos os pontos (anti-flicker e alternarTema) devem usar o papel quente
        self.assertEqual(BASE.count("'#f8fafc'"), 0)


class TestTemaEscuroNoiteFria(unittest.TestCase):
    def test_accent_musgo_clareado(self):
        self.assertIn("--accent: #4a8a75", BASE)

    def test_danger_terracota_clara(self):
        self.assertIn("--danger: #d29a85", BASE)

    def test_warning_ocre_clara(self):
        self.assertIn("--warning: #d4b26b", BASE)


class TestGradientesThemeAware(unittest.TestCase):
    """Novas variáveis por tema que substituem os gradientes hardcoded âmbar."""

    def test_brand_grad_var(self):
        self.assertIn("--brand-grad", BASE)

    def test_stat_bars_vars(self):
        for var in ("--stat-bar-gold", "--stat-bar-danger",
                    "--stat-bar-warning", "--stat-bar-success"):
            self.assertIn(var, BASE)

    def test_nenhum_ambar_hardcoded_nos_gradientes(self):
        # O âmbar sai do DNA: gradientes de brand/stat-card/btn/FAB não usam mais #f59e0b
        for snippet in (
            "linear-gradient(135deg, #f59e0b, #d97706)",   # brand-icon
            "linear-gradient(90deg, #f59e0b, #d97706)",    # stat-card accent-gold
            "background: linear-gradient(135deg, #f59e0b, #d97706);\n            border: none;\n            color: #ffffff !important;\n            box-shadow: 0 2px 8px rgba(245, 158, 11, 0.3)",  # btn-primary
        ):
            self.assertNotIn(snippet, BASE)


class TestGraficosThemeAware(unittest.TestCase):
    def test_chart_colors_retornam_entrada_saida(self):
        self.assertIn("entrada:", DASH)
        self.assertIn("saida:", DASH)

    def test_chart_palette_musgo_terracota_ocre(self):
        self.assertIn("#2f6f5e", DASH)
        self.assertIn("#b0705a", DASH)
        self.assertIn("#a5822f", DASH)

    def test_datasets_usam_colors(self):
        self.assertIn("backgroundColor: colors.entrada", DASH)
        self.assertIn("backgroundColor: colors.saida", DASH)
        self.assertIn("backgroundColor: colors.meta", DASH)
        self.assertIn("backgroundColor: colors.gastosPalette", DASH)

    def test_thememchange_repinta_datasets(self):
        self.assertIn("chartEvolucaoInstance.data.datasets[0].backgroundColor = colors.entrada", DASH)
        self.assertIn("chartGastosInstance.data.datasets[0].backgroundColor = colors.gastosPalette", DASH)
        self.assertIn("chartMetasInstance.data.datasets[0].backgroundColor = colors.meta", DASH)


class UIRedesignTest(unittest.TestCase):
    def test_external_resources_are_versioned_and_use_sri(self):
        base = (BASE_DIR / 'templates' / 'base.html').read_text(encoding='utf-8')
        for url in ('bootstrap@5.3.3/dist/css/bootstrap.min.css',
                    'bootstrap-icons@1.11.3/font/bootstrap-icons.min.css',
                    'bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js',
                    'chart.js@4.4.4/dist/chart.umd.min.js'):
            self.assertIn(url, base)
        for sri in (
            'sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH',
            'sha384-XGjxtQfXaH2tnPFa9x+ruJTuLE3Aa6LhHSWRr1XeTyhezb4abCG4ccI5AkVDxqC+',
            'sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz',
            'sha384-NrKB+u6Ts6AtkIhwPixiKTzgSKNblyhlk0Sohlgar9UHUBzai/sgnNNWWd291xqt'
        ):
            self.assertIn(sri, base)
        self.assertEqual(base.count('crossorigin="anonymous"'), 4)


    def test_shared_motion_tokens_and_reduced_motion(self):
        base = (BASE_DIR / 'templates' / 'base.html').read_text(encoding='utf-8')
        css = (BASE_DIR / 'static' / 'mobile-refinement.css').read_text(encoding='utf-8')
        dash = (BASE_DIR / 'templates' / 'dashboard.html').read_text(encoding='utf-8')

        # Shared motion tokens
        self.assertIn('--motion-fast: 150ms;', base)
        self.assertIn('--motion-normal: 250ms;', base)
        self.assertIn('--motion-ease: cubic-bezier(0.16, 1, 0.3, 1);', base)

        # Transitions using motion tokens
        self.assertIn('var(--motion-normal)', base)

        # Reduced motion in mobile-refinement.css
        self.assertIn('scroll-behavior: auto !important;', css)
        self.assertIn('animation-duration: .01ms !important;', css)

        # Gestures skip animated drag transforms under reduced motion
        self.assertIn("typeof reducedMotion !== 'undefined' && reducedMotion.matches", base)

        # Chart.js animation preference
        self.assertIn('animation: reducedMotion.matches ? false : undefined', dash)


class ChartAccessibilityTests(unittest.TestCase):
    def test_auth_reuses_fonts_colors_and_focus(self):
        login = (BASE_DIR / 'templates/login.html').read_text(encoding='utf-8')
        for token in ("--font-display: 'Fraunces', Georgia, serif", "--font-body: 'Karla', sans-serif",
                      '#f7f4ee', '#fffdf7', '#2f6f5e', '#2b2620', '#0c0e12', '#14181f',
                      'prefers-color-scheme', ':focus-visible', 'var(--border-focus)',
                      'family=Fraunces', 'family=Karla'):
            self.assertIn(token, login)
        self.assertNotIn('Plus Jakarta', login)

    def test_submit_feedback_and_focus_contract(self):
        css = (BASE_DIR / 'static/mobile-refinement.css').read_text(encoding='utf-8')
        self.assertIn('outline: 3px solid var(--border-focus)', css)
        for name in ('_pagamento', 'importar', 'nova_pendencia', 'nova_meta', 'editar_meta', 'metas', 'pendencias'):
            html = (BASE_DIR / f'templates/{name}.html').read_text(encoding='utf-8')
            self.assertIn('data-submit-feedback', html)

    def test_canvas_names_and_text_equivalents(self):
        import re
        for chart, name in [('Evolucao', 'evolucao'), ('Gastos', 'gastos'), ('Metas', 'metas')]:
            canvas = re.search(r'<canvas[^>]+id="grafico' + chart + r'"[^>]*>', DASH).group()
            self.assertIn('role="img"', canvas)
            self.assertIn(f'aria-labelledby="titulo-{name}"', canvas)
            self.assertIn(f'aria-describedby="resumo-{name}"', canvas)
            self.assertIn(f'id="titulo-{name}"', DASH)
            self.assertIn(f'id="resumo-{name}"', DASH)
        report = (BASE_DIR / 'templates/relatorios.html').read_text(encoding='utf-8')
        self.assertIn('aria-describedby="detalhamento-categorias"', report)
        self.assertIn('id="detalhamento-categorias"', report)
        self.assertIn('animation: reducedMotion.matches ? false : undefined', report)
        self.assertIn("reducedMotion.addEventListener('change'", report)

    def test_dashboard_independent_states(self):
        self.assertNotIn('Promise.all([', DASH)
        for name in ('evolucao', 'gastos', 'metas'):
            self.assertIn(f'id="componente-{name}" data-state="loading" aria-busy="true"', DASH)
        self.assertIn('id="fechamentoMesSection" data-state="loading" aria-busy="true"', DASH)
        self.assertEqual(DASH.count("carregarComponente('/api/"), 4)
        self.assertIn('if (!response.ok)', DASH)
        self.assertIn("reducedMotion.addEventListener('change'", DASH)


if __name__ == "__main__":
    unittest.main()
