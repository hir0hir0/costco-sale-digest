"""costco パッケージのテスト。

抽出はどうしてもヒューリスティックなので、実際に踏んだ誤読（「1.13kg」を日付と
読む、値引き額を売価と読む等）を回帰テストとして残しておく。

    python -m unittest discover -s tests -v
"""

import email
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from costco import parse, sources  # noqa: E402
from costco.models import Offer, guess_category, offer_key  # noqa: E402
from costco.build import build_site  # noqa: E402
from costco.store import Store  # noqa: E402

BASE = date(2026, 8, 12)


class TestPrices(unittest.TestCase):
    def test_find_prices(self):
        self.assertEqual(parse.find_prices("通常1,980円 → セール価格 1,580円"), [1980, 1580])
        self.assertEqual(parse.find_prices("¥2,480"), [2480])
        self.assertEqual(parse.find_prices("2万9800円"), [29800])

    def test_weight_is_not_a_price(self):
        self.assertEqual(parse.find_prices("ミックスナッツ 1.13kg"), [])
        self.assertEqual(parse.find_prices("内容量 500g"), [])

    def test_split_prices_uses_context(self):
        self.assertEqual(parse.split_prices("通常1,980円 → 1,580円"), (1580, 1980))
        self.assertEqual(parse.split_prices("¥2,480"), (2480, None))

    def test_discount_amount_is_not_a_price(self):
        # 「¥69,800（¥15,000引き）」の 15,000 は売価ではない
        self.assertEqual(parse.split_prices("¥69,800 （¥15,000引き）", exclude=15000),
                         (69800, None))


class TestDiscount(unittest.TestCase):
    def test_yen(self):
        self.assertEqual(parse.find_discount("500円引き")[0], 500)
        self.assertEqual(parse.find_discount("¥1,000 OFF")[0], 1000)

    def test_percent(self):
        self.assertAlmostEqual(parse.find_discount("20%OFF")[1], 0.2)


class TestPeriod(unittest.TestCase):
    def test_slash_range(self):
        self.assertEqual(parse.find_period("セール期間 8/1(土)～8/14(金)", BASE),
                         ("2026-08-01", "2026-08-14"))

    def test_kanji_range(self):
        self.assertEqual(parse.find_period("2026年8月1日(土)〜8月14日(金)", BASE),
                         ("2026-08-01", "2026-08-14"))

    def test_iso_range(self):
        self.assertEqual(parse.find_period("2026-08-01 から 2026-08-14", BASE),
                         ("2026-08-01", "2026-08-14"))

    def test_end_only(self):
        self.assertEqual(parse.find_period("8月14日まで", BASE), ("", "2026-08-14"))

    def test_year_rollover(self):
        # 12月に見た「1/8まで」は翌年
        self.assertEqual(parse.find_period("1/8まで", date(2026, 12, 28)),
                         ("", "2027-01-08"))

    def test_weight_is_not_a_date(self):
        self.assertEqual(parse.find_period("ミックスナッツ 1.13kg 1,980円", BASE), ("", ""))


class TestItemNo(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse.find_item_no("商品番号: 1234567"), "1234567")
        self.assertEqual(parse.find_item_no("品番 98765"), "98765")
        self.assertEqual(parse.find_item_no("Item # 445566"), "445566")
        self.assertEqual(parse.find_item_no("内容量 1.13kg"), "")


class TestOffersFromLines(unittest.TestCase):
    LINES = [
        "今週のお買い得",
        "セール期間 8/1(土)～8/14(金)",
        "カークランドシグネチャー ミックスナッツ 1.13kg",
        "通常 2,480円 → 1,980円",
        "商品番号 1234567",
        "ダイソン V12 コードレスクリーナー",
        "¥69,800 （¥15,000引き）",
        "バウンティ ペーパータオル 12ロール",
        "3,280円",
        "商品番号 55555",
        "会員ログイン",
    ]

    def setUp(self):
        self.offers = {o.name: o for o in
                       parse.offers_from_lines(self.LINES, source="mail", base=BASE)}

    def test_three_products(self):
        self.assertEqual(len(self.offers), 3)

    def test_prices_and_period(self):
        o = self.offers["カークランドシグネチャー ミックスナッツ 1.13kg"]
        self.assertEqual((o.price, o.regular_price, o.discount), (1980, 2480, 500))
        self.assertEqual((o.starts_on, o.ends_on), ("2026-08-01", "2026-08-14"))
        self.assertEqual(o.item_no, "1234567")

    def test_discount_not_mistaken_for_price(self):
        o = self.offers["ダイソン V12 コードレスクリーナー"]
        self.assertEqual((o.price, o.discount, o.regular_price), (69800, 15000, 84800))

    def test_item_no_does_not_leak_to_neighbour(self):
        # 隣の商品の商品番号を掴んでいないこと
        self.assertEqual(self.offers["ダイソン V12 コードレスクリーナー"].item_no, "")
        self.assertEqual(self.offers["バウンティ ペーパータオル 12ロール"].item_no, "55555")

    def test_boilerplate_is_not_a_product(self):
        self.assertNotIn("会員ログイン", self.offers)

    def test_page_period_fills_in(self):
        # 期間はページ先頭に1回しか書かれていなくても全商品に効く
        for o in self.offers.values():
            self.assertEqual(o.ends_on, "2026-08-14")


class TestRealNewsletterShape(unittest.TestCase):
    """実際のコストコのメルマガで踏んだ形。

    価格が「¥」と「7,358」で別の要素に分かれており、商品ごとに `ITEM #12345`
    が付く。この形で抽出0件になった。
    """

    # 「Price / 金額 / Off / 金額 / 売価」とラベルが金額の前に来る形。
    LINES = [
        "CostcoEDM_20260807",
        "メールが正しく表示されない方は、こちら をご覧ください。",
        "Costco Wholesale",
        "Ka POD 部屋干しEX 衣料用洗濯洗剤 ジェル マジック ボール 120粒 本体",
        "Ball type Laundry Detergent for Indoor Drying Gel Magic Ball",
        "ITEM #80015",
        "Price",
        "¥3,498",
        "Off",
        "¥680",
        "¥2,818",
        "Shop Now >",
        "Hisense 100型 MiniLED 4K 液晶テレビ",
        '100" TV 100U7S',
        "ITEM #89130",
        "¥178,000",
        "Shop Now >",
    ]

    # ラベルが無く、通常価格→セール価格が並ぶだけの形。
    TWO_PRICES = [
        "Hisense 100型 量子ドット4K液晶スマートテレビ",
        '100" TV 100U7N',
        "ITEM #69180",
        "¥459,800",
        "Shop Now >",
        "¥329,800",
        "Shop Now >",
    ]

    def _offers(self, lines):
        return {o.item_no: o for o in
                parse.offers_from_item_numbers(lines, source="mail", base=BASE)}

    def test_labelled_prices(self):
        o = self._offers(self.LINES)["80015"]
        self.assertEqual((o.price, o.regular_price, o.discount), (2818, 3498, 680))

    def test_two_bare_prices_cheaper_is_the_sale_price(self):
        o = self._offers(self.TWO_PRICES)["69180"]
        self.assertEqual((o.price, o.regular_price), (329800, 459800))

    def test_prices_from_block(self):
        self.assertEqual(
            parse.prices_from_block(["Price", "¥3,498", "Off", "¥680", "¥2,818"]),
            (2818, 3498, 680))
        self.assertEqual(parse.prices_from_block(["¥459,800", "Shop Now >"]),
                         (459800, None, None))
        # 売価が書かれず「通常価格」と「値引き額」だけの場合は引き算する
        self.assertEqual(parse.prices_from_block(["通常", "¥1,000", "Off", "¥300"]),
                         (700, 1000, 300))

    def test_implausible_pair_is_not_treated_as_a_discount(self):
        # 隣の商品の金額を掴むと「¥15,980（通常 ¥158,000）＝90%引き」になる。
        # 対にせず、商品番号に近い最初の金額だけを採る。
        self.assertEqual(parse.prices_from_block(["¥15,980", "Shop Now >", "¥158,000"]),
                         (15980, None, None))
        # 常識的な範囲なら対にしてよい
        self.assertEqual(parse.prices_from_block(["¥3,000", "¥2,000"]),
                         (2000, 3000, None))

    def test_labelled_price_must_agree_with_the_subtraction(self):
        # 通常¥467,800 / 値引き¥93,560 なのに売価が ¥46,580 は隣の商品の金額。
        # 引き算と合わない金額は採らない。
        block = ["Price", "¥467,800", "Off", "¥93,560", "¥46,580"]
        price, regular, off = parse.prices_from_block(block)
        self.assertEqual((price, regular, off), (374240, 467800, 93560))

    def test_regular_only_with_unrelated_cheap_price(self):
        # 「Price ¥158,000」の後に無関係な ¥1,498 が続いても割引扱いしない
        price, regular, off = parse.prices_from_block(["Price", "¥158,000", "¥1,498"])
        self.assertEqual((price, regular, off), (158000, None, None))

    def test_currency_split_across_lines_is_rejoined(self):
        html = ("<table><tr><td>ITEM #28137</td></tr>"
                "<tr><td><span>¥</span><span>7,358</span></td></tr></table>")
        lines = parse.html_to_lines(html)
        self.assertIn("¥7,358", lines)

    def test_item_number_anchored_extraction(self):
        self.assertEqual(set(self._offers(self.LINES)), {"80015", "89130"})

    def test_japanese_name_wins_over_english(self):
        got = self._offers(self.LINES)
        self.assertEqual(
            got["80015"].name,
            "Ka POD 部屋干しEX 衣料用洗濯洗剤 ジェル マジック ボール 120粒 本体")
        self.assertEqual(got["89130"].name, "Hisense 100型 MiniLED 4K 液晶テレビ")

    def test_next_products_price_does_not_leak_backwards(self):
        got = self._offers(self.LINES)
        self.assertEqual(got["89130"].price, 178000)
        self.assertNotEqual(got["80015"].price, 178000)


class TestTwoColumnLayout(unittest.TestCase):
    """2列レイアウトのメルマガ（2026-08-24「Back to School」で実際に踏んだ形）。

    商品番号が金額を挟まず隣接し、価格ブロックは**まとめて後ろに左から順**で
    並ぶ。「商品番号の直後の価格＝その商品」と読むと、1番目の価格が2番目に
    付き、TEMPURの枕が隣のシーツの1,748円になった。
    """

    LINES = [
        "西川 のびのびクイック シーツ シングルサイズ",
        "TEMPUR オリジナルネックピロー",
        "HOT BUY",
        "西川 のびのびクイック シーツ シングルサイズ 各色",
        "Nishikawa Stretchable Quick Sheet",
        "ITEM #68380",
        "HOT BUY",
        "TEMPUR オリジナルネックピロー",
        "Original Neck Pillow",
        "ITEM #587900",
        "Price",
        "¥2,298",
        "Off",
        "¥550",
        "¥1,748",
        "Shop Now >",
        "¥ 1,440 OFF",
        "Shop Now >",
        "オキシクリーン 5.26kg",
        "ミューズ 泡ハンドソープ 詰替え用 4.8L",
        "HOT BUY",
        "オキシクリーン 5.26kg",
        "Oxiclean Max Efficiency",
        "ITEM #28137",
        "HOT BUY",
        "ミューズ 泡ハンドソープ 詰替え用 4.8L",
        "MUSE Foam Hand Soap Refill 4.8L",
        "ITEM #53223",
        "Price",
        "¥3,498",
        "Off",
        "¥680",
        "¥2,818",
        "定期購入対象",
        "Shop Now >",
        "Price",
        "¥4,198",
        "Off",
        "¥840",
        "¥3,358",
        "Shop Now >",
    ]

    def setUp(self):
        self.got = {o.item_no: o for o in parse.offers_from_item_numbers(
            self.LINES, source="mail", base=BASE)}

    def test_first_of_pair_gets_the_first_price_block(self):
        o = self.got["68380"]   # 西川シーツ
        self.assertEqual((o.price, o.regular_price, o.discount), (1748, 2298, 550))
        o = self.got["28137"]   # オキシクリーン
        self.assertEqual((o.price, o.regular_price, o.discount), (2818, 3498, 680))

    def test_second_of_pair_gets_the_second_price_block(self):
        o = self.got["587900"]  # TEMPUR: 値引きだけの号
        self.assertEqual(o.price, None)
        self.assertEqual(o.discount, 1440)
        o = self.got["53223"]   # ミューズ
        self.assertEqual((o.price, o.regular_price, o.discount), (3358, 4198, 840))

    def test_temppur_does_not_steal_the_sheets_price(self):
        # まさに起きた誤り: TEMPURの枕が隣のシーツの1,748円になる
        self.assertNotEqual(self.got["587900"].price, 1748)

    def test_names_are_right(self):
        self.assertIn("シーツ", self.got["68380"].name)
        self.assertIn("TEMPUR", self.got["587900"].name)


class TestImages(unittest.TestCase):
    HTML = (
        "<div><img src='https://cdn.example.com/oxi.jpg' alt='オキシクリーン'></div>"
        "<div>オキシクリーン 5.26kg</div>"
        "<div>ITEM #28137</div>"
        "<div><span>¥</span><span>2,898</span></div>"
        "<div><img src='https://cdn.example.com/tv.jpg' alt='テレビ'></div>"
        "<div>Hisense 55型 4K 液晶テレビ</div>"
        "<div>ITEM #69040</div>"
        "<div>¥59,800</div>"
    )

    def test_images_are_kept_out_of_the_text(self):
        lines, images = parse.html_to_lines_with_images(self.HTML)
        self.assertFalse(any("cdn.example.com" in l for l in lines))
        self.assertEqual([src for _, src, _ in images],
                         ["https://cdn.example.com/oxi.jpg",
                          "https://cdn.example.com/tv.jpg"])

    def test_price_merge_still_works_with_images_present(self):
        lines, _ = parse.html_to_lines_with_images(self.HTML)
        self.assertIn("¥2,898", lines)

    def test_each_product_gets_its_own_image(self):
        lines, images = parse.html_to_lines_with_images(self.HTML)
        got = {o.item_no: o for o in parse.offers_from_item_numbers(
            lines, source="mail", base=BASE, images=images)}
        self.assertEqual(got["28137"].image_url, "https://cdn.example.com/oxi.jpg")
        self.assertEqual(got["69040"].image_url, "https://cdn.example.com/tv.jpg")

    def test_image_far_above_the_item_number_is_still_found(self):
        # カードの先頭に画像があり、商品名まで数行あく形。
        lines = ["ロゴ",
                 parse.IMG_MARK + "https://cdn.example.com/logo.gif",
                 "【 Hot Buy 】", "対象商品", "期間中", "数量限定", "本体",
                 parse.IMG_MARK + "https://cdn.example.com/item.jpg",
                 "オキシクリーン 5.26kg", "Oxiclean", "ITEM #28137", "¥2,898"]
        text, images = [], []
        for ln in lines:
            if ln.startswith(parse.IMG_MARK):
                src, _, alt = ln[len(parse.IMG_MARK):].partition(parse.IMG_SEP)
                images.append((len(text), src, alt))
            else:
                text.append(ln)
        got = parse.offers_from_item_numbers(text, source="mail", base=BASE,
                                             images=images)
        self.assertEqual(got[0].image_url, "https://cdn.example.com/item.jpg")

    def _build(self, images):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = Store(Path(d.name) / "data")
        store.merge([Offer(name="テスト", item_no="1", price=100,
                           ends_on="2026-08-31", image="abc123.jpg",
                           image_url="https://cdn.example.com/x.jpg")], BASE)
        out = Path(d.name) / "site"
        build_site(store, out, on=BASE, images=images)
        return (out / "index.html").read_text(encoding="utf-8")

    def test_download_mode_uses_relative_paths(self):
        html = self._build("download")
        self.assertIn("img/abc123.jpg", html)
        self.assertNotIn("cdn.example.com", html)

    def test_link_mode_uses_the_source_url(self):
        # ダウンロードせず、メルマガに書かれた画像URLを直接参照する
        html = self._build("link")
        self.assertIn("https://cdn.example.com/x.jpg", html)
        self.assertNotIn("img/abc123.jpg", html)

    def test_off_mode_has_no_images(self):
        html = self._build("off")
        self.assertNotIn("cdn.example.com", html)
        self.assertNotIn("abc123.jpg", html)


class TestJsonLd(unittest.TestCase):
    HTML = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"テスト商品",
     "sku":"9988776","offers":{"@type":"Offer","price":"1280.00",
     "priceCurrency":"JPY","priceValidUntil":"2026-08-31"}}
    </script></head><body>本文</body></html>
    """

    def test_extracts_product(self):
        got = parse.offers_from_json_ld(self.HTML, source="web", base=BASE)
        self.assertEqual(len(got), 1)
        o = got[0]
        self.assertEqual((o.name, o.item_no, o.price, o.ends_on),
                         ("テスト商品", "9988776", 1280, "2026-08-31"))
        self.assertGreater(o.confidence, 0.9)


class TestHtmlToLines(unittest.TestCase):
    def test_script_is_dropped(self):
        html = "<div>商品A</div><script>var x='1,000円';</script><p>2,000円</p>"
        lines = parse.html_to_lines(html)
        self.assertIn("商品A", lines)
        self.assertIn("2,000円", lines)
        self.assertFalse(any("var x" in l for l in lines))

    def test_unclosed_head_does_not_swallow_the_page(self):
        # HTML5 では </head> を省略してよい。省略されたページで本文を丸ごと
        # 捨ててしまい、抽出0件になった（実サイトで踏んだ）。
        html = ("<html><head><title>T</title><meta charset='utf-8'>"
                "<body><div>商品A</div><p>2,000円</p></body></html>")
        lines = parse.html_to_lines(html)
        self.assertIn("商品A", lines)
        self.assertIn("2,000円", lines)

    def test_falls_back_when_nothing_was_extracted(self):
        # <style> を閉じ忘れると（ブラウザ同様）以降が全部スタイル扱いになる。
        # 何も取れなかったら正規表現で剥がし直す保険が効くこと。
        html = ("<html><head><style>.a{color:red}" + "/* " + "x" * 2500 + " */"
                "<body><div>商品B</div><p>3,000円</p></body></html>")
        lines = parse.html_to_lines(html)
        self.assertIn("商品B", lines)
        self.assertIn("3,000円", lines)


class TestModels(unittest.TestCase):
    def test_key_prefers_item_no(self):
        self.assertEqual(offer_key("1234567", "なにか"), "no:1234567")

    def test_key_absorbs_spacing(self):
        self.assertEqual(offer_key("", "カークランド ミックスナッツ"),
                         offer_key("", "カークランド　ミックスナッツ"))

    def test_category_guess(self):
        self.assertEqual(guess_category("カークランド ミックスナッツ 1.13kg"), "お菓子")
        self.assertEqual(guess_category("バウンティ ペーパータオル"), "日用品")

    def test_third_price_field_is_derived(self):
        o = Offer(name="x", price=1580, regular_price=1980).normalize(BASE)
        self.assertEqual(o.discount, 400)
        o2 = Offer(name="x", regular_price=1980, discount=400).normalize(BASE)
        self.assertEqual(o2.price, 1580)

    def test_days_left(self):
        o = Offer(name="x", ends_on="2026-08-14").normalize(BASE)
        self.assertEqual(o.days_left(BASE), 2)
        self.assertTrue(o.is_active(BASE))
        self.assertFalse(o.is_active(date(2026, 8, 20)))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _offer(self, price, ends="2026-08-14"):
        return Offer(name="カークランド ミックスナッツ", item_no="1234567",
                     price=price, ends_on=ends, source="mail")

    def test_merge_then_price_drop(self):
        r1 = self.store.merge([self._offer(1980)], date(2026, 8, 1))
        self.assertEqual(len(r1.added), 1)
        self.assertEqual(r1.price_drops, [])

        r2 = self.store.merge([self._offer(1780)], date(2026, 8, 5))
        self.assertEqual(len(r2.added), 0)
        self.assertEqual([d for _, d in r2.price_drops], [200])
        self.assertEqual(len(r2.lowest_ever), 1)

        stats = self.store.price_stats("no:1234567")
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["lowest"], 1780)
        self.assertEqual(stats["prev"], 1980)

    def test_same_price_does_not_add_a_point(self):
        self.store.merge([self._offer(1980)], date(2026, 8, 1))
        self.store.merge([self._offer(1980)], date(2026, 8, 2))
        self.assertEqual(self.store.price_stats("no:1234567")["count"], 1)

    def test_two_newsletters_in_one_batch_do_not_zigzag_history(self):
        # 同じ商品が2つの号に違う価格で載っていても、1回の収集は履歴1点。
        # 後の号（＝新しいメール）の価格が正になる。
        self.store.merge([self._offer(1980), self._offer(1780)], date(2026, 8, 1))
        stats = self.store.price_stats("no:1234567")
        self.assertEqual(stats["count"], 1)
        self.assertEqual(self.store.offers[0].price, 1780)

    def test_history_keeps_one_point_per_day(self):
        # 同じ日に何度収集しても点は増えない。偽の「過去最安」も出ない。
        d = date(2026, 8, 1)
        self.store.merge([self._offer(1980)], d)
        r2 = self.store.merge([self._offer(1780)], d)
        r3 = self.store.merge([self._offer(1980)], d)
        self.assertEqual(self.store.price_stats("no:1234567")["count"], 1)
        self.assertEqual(r2.lowest_ever, [])
        self.assertEqual(r3.price_drops, [])
        # 日をまたいで本当に下がったときだけ検知する
        r4 = self.store.merge([self._offer(1780)], date(2026, 8, 2))
        self.assertEqual([x for _, x in r4.price_drops], [200])

    def test_separate_runs_are_separate_offers(self):
        self.store.merge([self._offer(1980, ends="2026-08-14")], date(2026, 8, 1))
        self.store.merge([self._offer(1880, ends="2026-10-14")], date(2026, 10, 1))
        self.assertEqual(len(self.store.offers), 2)

    def test_roundtrip(self):
        self.store.merge([self._offer(1980)], date(2026, 8, 1))
        self.store.save()
        again = Store.load(Path(self.tmp.name))
        self.assertEqual(len(again.offers), 1)
        self.assertEqual(again.offers[0].price, 1980)

    def test_prune_drops_old_runs_but_keeps_history(self):
        self.store.merge([self._offer(1980, ends="2026-01-10")], date(2026, 1, 1))
        removed = self.store.prune(keep_days=30, on=date(2026, 8, 12))
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.price_stats("no:1234567")["count"], 1)


class TestBackfill(unittest.TestCase):
    def test_chronological_merge_dates_the_history_correctly(self):
        # バックフィルは昔のメールを当時の日付で時系列に流し込む。
        # 履歴の点にメールの日付が付き、値下がりも当時の日付間で検知される。
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            def offer(price):
                return Offer(name="ミックスナッツ", item_no="1234567",
                             price=price, ends_on="2026-08-31", source="mail")
            store.merge([offer(1980)], date(2026, 7, 25))
            r = store.merge([offer(1780)], date(2026, 8, 10))
            store.merge([offer(1780)], date(2026, 8, 24))
            pts = store.price_stats("no:1234567")["points"]
            self.assertEqual([(p["on"], p["price"]) for p in pts],
                             [("2026-07-25", 1980), ("2026-08-10", 1780)])
            self.assertEqual([x for _, x in r.price_drops], [200])


class TestReceipts(unittest.TestCase):
    PURCHASES = [{
        "date": "2026-08-01", "store": "◯◯", "total": 3000,
        "items": [
            {"item_no": "588141", "name": "ハンドソープ 4P", "price": 1998, "coupon": 420},
            {"item_no": "30669", "name": "バナナ", "price": 328},
        ],
    }]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "data")
        self.store.root.mkdir(parents=True)
        (self.store.root / "purchases.json").write_text(
            json.dumps(self.PURCHASES, ensure_ascii=False), encoding="utf-8")

    def test_receipt_prices_enter_history_dated_and_idempotent(self):
        self.assertEqual(self.store.merge_receipts(), 2)
        self.assertEqual(self.store.merge_receipts(), 2)   # 二度読んでも増えない
        pts = self.store.price_stats("no:588141")["points"]
        self.assertEqual([(p["on"], p["price"]) for p in pts], [("2026-08-01", 1998)])

    def test_historical_insert_keeps_points_sorted(self):
        # 先にメルマガの点（8/12）があり、後からレシート（8/1）を入れても順序が保たれ、
        # 偽の値下がり通知は出ない
        self.store.merge([Offer(name="ハンドソープ 4P", item_no="588141",
                                price=2298, source="mail")], BASE)
        self.store.merge_receipts()
        pts = self.store.price_stats("no:588141")["points"]
        self.assertEqual([p["on"] for p in pts], ["2026-08-01", "2026-08-12"])

    def test_site_shows_last_bought_and_badge(self):
        # クーポン込みの実質額（1998-420=1578）より安いセールにだけバッジが付く
        self.store.merge([Offer(name="ハンドソープ 4P", item_no="588141",
                                price=1498, ends_on="2026-08-31", source="mail")], BASE)
        out = Path(self.tmp.name) / "site"
        build_site(self.store, out, on=BASE)
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        o = next(x for x in data["offers"] if x["item_no"] == "588141")
        self.assertEqual(o["last_bought"], {"month": "2026-08", "price": 1578})
        self.assertTrue(o["below_last_buy"])
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("前回購入より安い", html)

    def test_public_data_never_leaks_store_day_or_total(self):
        # personas.md の公開ポリシー: 店舗は出さない・購入日は月単位・数量と合計は出さない。
        # purchases.json に何を足しても、公開物に素通しさせない（allowlist）
        (self.store.root / "purchases.json").write_text(json.dumps([{
            "date": "2026-08-29", "store": "◯◯倉庫店", "total": *****,
            "member_no": "1234567890", "payment": "VISA ****4321",
            "items": [{"item_no": "588141", "name": "ハンドソープ 4P",
                       "price": 1998, "coupon": 420, "register": "12-3"}],
        }], ensure_ascii=False), encoding="utf-8")
        self.store.merge_receipts()
        out = Path(self.tmp.name) / "site2"
        build_site(self.store, out, on=BASE)
        for blob in ((out / "data.json").read_text(encoding="utf-8"),
                     (out / "index.html").read_text(encoding="utf-8"),
                     json.dumps(self.store.history, ensure_ascii=False)):
            for secret in ("◯◯", "2026-08-29", "*****", "1234567890", "4321", "12-3"):
                self.assertNotIn(secret, blob)
        rec = json.loads((out / "data.json").read_text(encoding="utf-8"))["purchases"][0]
        self.assertEqual(sorted(rec), ["items", "month"])
        self.assertEqual(rec["month"], "2026-08")

    def test_same_month_duplicate_item_collapses_to_one_row(self):
        # 同月に同じ商品が複数行あると数量が復元できてしまうので1行に潰す。
        # 金額が違う＝量り売り（総額は重さ次第）なので比較から外す
        (self.store.root / "purchases.json").write_text(json.dumps([{
            "month": "2026-08",
            "items": [{"item_no": "90223", "name": "カルビ焼肉", "price": 2374, "coupon": 300},
                      {"item_no": "90223", "name": "カルビ焼肉", "price": 2313, "coupon": 300}],
        }], ensure_ascii=False), encoding="utf-8")
        items = self.store.public_purchases()[0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["price"], 2313)
        self.assertTrue(items[0]["weighed"])
        self.store.merge_receipts()
        # weighed の点は他の点と比べられないので、底値・前回比較に混ぜない
        self.assertEqual(self.store.price_stats("no:90223")["points"], [])

    def test_unit_price_makes_weighed_items_comparable(self):
        # 単価が分かっていれば量り売りでも比べられる。安い方（¥/100g）を採る
        (self.store.root / "purchases.json").write_text(json.dumps([{
            "month": "2026-08",
            "items": [{"item_no": "90223", "name": "カルビ焼肉", "price": 2374,
                       "coupon": 0, "weight_g": 500},
                      {"item_no": "90223", "name": "カルビ焼肉", "price": 2313,
                       "coupon": 0, "weight_g": 450}],
        }], ensure_ascii=False), encoding="utf-8")
        items = self.store.public_purchases()[0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["price"], 2374)      # 475円/100g < 514円/100g
        self.assertNotIn("weighed", items[0])
        self.store.merge_receipts()
        pt = self.store.history["no:90223"]["points"][0]
        self.assertEqual(pt["unit_price"], 475)


class TestStaleHiding(unittest.TestCase):
    def test_endless_offers_disappear_after_30_days_unseen(self):
        # 終了日の無いセールは、30日メールに現れなければサイトから外す
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "data")
            store.merge([Offer(name="古いセール", item_no="1", price=100)],
                        date(2026, 7, 1))
            store.merge([Offer(name="新しいセール", item_no="2", price=200)],
                        date(2026, 8, 20))
            out = Path(d) / "site"
            build_site(store, out, on=BASE)   # BASE = 2026-08-12 → 古い方は42日前
            data = json.loads((out / "data.json").read_text(encoding="utf-8"))
            names = [o["name"] for o in data["offers"]]
            self.assertIn("新しいセール", names)
            self.assertNotIn("古いセール", names)
            self.assertEqual(data["hidden_stale"], 1)


class TestMail(unittest.TestCase):
    RAW = (
        "From: Costco Japan <news@costco.co.jp>\r\n"
        "Subject: =?UTF-8?B?5LuK6YCx44Gu44GK6LK344GE5b6X?=\r\n"
        "Date: Wed, 12 Aug 2026 09:00:00 +0900\r\n"
        "Content-Type: text/html; charset=UTF-8\r\n"
        "\r\n"
        "<html><body>"
        "<p>セール期間 8/10(月)〜8/23(日)</p>"
        "<div>カークランドシグネチャー オリーブオイル 2L</div>"
        "<div>通常 2,980円 <b>2,380円</b></div>"
        "<div>商品番号 1583333</div>"
        "</body></html>"
    )

    def test_extracts_offer_from_message(self):
        # 実際のIMAP取得と同じくバイト列から組み立てる。str から作ると
        # get_payload(decode=True) が raw-unicode-escape で本文を壊す。
        msg = email.message_from_bytes(self.RAW.encode("utf-8"))
        got = sources.offers_from_message(msg, base=BASE)
        self.assertEqual(len(got), 1)
        o = got[0]
        self.assertEqual(o.name, "カークランドシグネチャー オリーブオイル 2L")
        self.assertEqual((o.price, o.regular_price), (2380, 2980))
        self.assertEqual((o.starts_on, o.ends_on), ("2026-08-10", "2026-08-23"))
        self.assertEqual(o.item_no, "1583333")
        self.assertEqual(o.source, "mail")
        self.assertIn("今週のお買い得", o.source_label)

    def test_eml_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.eml"
            p.write_bytes(self.RAW.encode("utf-8"))
            self.assertEqual(len(sources.offers_from_eml(p, base=BASE)), 1)


class TestBuild(unittest.TestCase):
    def test_site_is_self_contained(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "data")
            store.merge([
                Offer(name="カークランド ミックスナッツ 1.13kg", item_no="1234567",
                      price=1980, regular_price=2480, ends_on="2026-08-31", source="mail"),
                Offer(name="バウンティ ペーパータオル", price=3280,
                      ends_on="2026-08-20", source="web"),
            ], date(2026, 8, 1))
            store.merge([
                Offer(name="カークランド ミックスナッツ 1.13kg", item_no="1234567",
                      price=1780, regular_price=2480, ends_on="2026-08-31", source="mail"),
            ], BASE)
            out = Path(d) / "site"
            build_site(store, out, on=BASE)

            html = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("ミックスナッツ", html)
            # 外部リソースを読まない（CSP的にも、オフラインでも開ける）
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
            self.assertNotIn("<script src=", html)

            data = json.loads((out / "data.json").read_text(encoding="utf-8"))
            nuts = next(o for o in data["offers"] if o["item_no"] == "1234567")
            self.assertEqual(nuts["price"], 1780)
            self.assertEqual(nuts["prev_price"], 1980)
            self.assertTrue(nuts["is_lowest"])
            self.assertEqual(len(nuts["history"]), 2)


if __name__ == "__main__":
    unittest.main()
