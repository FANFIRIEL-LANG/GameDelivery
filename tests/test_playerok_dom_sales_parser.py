from pathlib import Path
import unittest

from integrations.playerok.dom_sales import parse_playerok_sales_dom


FIXTURE = Path(__file__).parent / "fixtures" / "playerok_sales_observed_two_cards.html"
FIRST_ID = "01234567-89ab-4def-8123-456789abcdef"
SECOND_ID = "01234567-89ab-4def-8123-456789abcdee"


def observed_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class PlayerokDomSalesParserTests(unittest.TestCase):
    def test_extracts_two_observed_cards_only_as_candidates(self) -> None:
        snapshot = parse_playerok_sales_dom(observed_html())

        self.assertEqual(
            [candidate.candidate_deal_id for candidate in snapshot.candidates],
            [FIRST_ID, SECOND_ID],
        )
        self.assertTrue(all(candidate.listing_id is None for candidate in snapshot.candidates))
        self.assertTrue(all(candidate.chat_id is None for candidate in snapshot.candidates))
        self.assertTrue(all(not hasattr(candidate, "deal_id") for candidate in snapshot.candidates))
        self.assertIs(snapshot.snapshot_complete, False)

    def test_ignores_unknown_attributes_and_descendants(self) -> None:
        html = observed_html().replace(
            '<a class="MuiTypography-root MuiLink-root"',
            '<a data-deal-id="untrusted" data-listing-id="untrusted" '
            'data-chat-id="untrusted" data-status="PAID" '
            'class="MuiTypography-root MuiLink-root"',
            1,
        ).replace(
            "[REDACTED_SECONDARY_TEXT]",
            '<span data-id="untrusted" data-cursor="untrusted">ignored</span>',
            1,
        )

        snapshot = parse_playerok_sales_dom(html)

        self.assertEqual(
            [candidate.candidate_deal_id for candidate in snapshot.candidates],
            [FIRST_ID, SECOND_ID],
        )
        self.assertTrue(all(candidate.listing_id is None for candidate in snapshot.candidates))
        self.assertTrue(all(candidate.chat_id is None for candidate in snapshot.candidates))
        self.assertIs(snapshot.snapshot_complete, False)

    def test_missing_or_non_uuid_deal_ids_are_ignored(self) -> None:
        html = """
        <div>
          <a class="MuiLink-root">no href</a>
          <a href="/deal/">empty route id</a>
          <a href="/deal/not-a-uuid">malformed route id</a>
          <a href="/deal/0123456789abcdef0123456789abcdef">wrong route shape</a>
          <a href="/products/01234567-89ab-4def-8123-456789abcdef">not a deal route</a>
        </div>
        """

        snapshot = parse_playerok_sales_dom(html)

        self.assertEqual(snapshot.candidates, ())
        self.assertIs(snapshot.snapshot_complete, False)

    def test_repeated_links_are_deduplicated_in_first_seen_order(self) -> None:
        html = observed_html() + f'<a href="/deal/{FIRST_ID}">duplicate</a>'

        snapshot = parse_playerok_sales_dom(html)

        self.assertEqual(
            [candidate.candidate_deal_id for candidate in snapshot.candidates],
            [FIRST_ID, SECOND_ID],
        )
        self.assertIs(snapshot.snapshot_complete, False)

    def test_completeness_markers_and_card_count_never_prove_snapshot_complete(self) -> None:
        html = observed_html() + (
            '<div data-total-count="2" data-has-next-page="false" '
            'data-end-of-list="true"></div>'
        )

        snapshot = parse_playerok_sales_dom(html)

        self.assertEqual(len(snapshot.candidates), 2)
        self.assertIs(snapshot.snapshot_complete, False)

    def test_accepts_same_origin_absolute_link_but_rejects_external_link(self) -> None:
        html = f"""
        <a href="https://playerok.com/deal/{FIRST_ID}">same origin</a>
        <a href="https://example.invalid/deal/{SECOND_ID}">external</a>
        """

        snapshot = parse_playerok_sales_dom(html)

        self.assertEqual(
            [candidate.candidate_deal_id for candidate in snapshot.candidates],
            [FIRST_ID],
        )
        self.assertIs(snapshot.snapshot_complete, False)

    def test_rejects_non_string_html(self) -> None:
        with self.assertRaises(TypeError):
            parse_playerok_sales_dom(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()