import unittest

import core_cross_sectional_neutralization as subject


class CrossSectionalNeutralizationTests(unittest.TestCase):
    def test_none_keeps_raw_scores(self):
        raw = {"AAA": 2.0, "BBB": -1.0}
        self.assertEqual(subject.neutralize_scores(raw, "NONE"), raw)

    def test_market_subtracts_cross_sectional_mean(self):
        result = subject.neutralize_scores(
            {"AAA": 3.0, "BBB": 1.0, "CCC": -1.0},
            "MARKET",
            metadata_by_ticker={},
        )
        self.assertAlmostEqual(sum(result.values()), 0.0)
        self.assertAlmostEqual(result["AAA"], 2.0)

    def test_sector_means_are_zero_independently(self):
        raw = {"AAA": 4.0, "BBB": 2.0, "CCC": 1.0, "DDD": -1.0}
        metadata = {
            "AAA": {"sector": "Tech"},
            "BBB": {"sector": "Tech"},
            "CCC": {"sector": "Health"},
            "DDD": {"sector": "Health"},
        }
        result = subject.neutralize_scores(raw, "SECTOR", metadata)
        self.assertAlmostEqual(result["AAA"] + result["BBB"], 0.0)
        self.assertAlmostEqual(result["CCC"] + result["DDD"], 0.0)

    def test_country_uses_listing_market_not_company_country(self):
        raw = {"ADR": 3.0, "CN": 1.0, "US": 10.0}
        metadata = {
            "ADR": {"company_country": "China", "listing_country": "US"},
            "CN": {"company_country": "China", "listing_country": "CH"},
            "US": {"company_country": "United States", "listing_country": "US"},
        }
        result = subject.neutralize_scores(raw, "COUNTRY", metadata)
        self.assertAlmostEqual(result["ADR"], -3.5)
        self.assertAlmostEqual(result["US"], 3.5)
        self.assertAlmostEqual(result["CN"], 0.0)


if __name__ == "__main__":
    unittest.main()
