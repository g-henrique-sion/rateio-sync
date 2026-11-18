import unittest
from decimal import Decimal
from unittest.mock import patch

from src.poll import (
    _apply_k_l_m_targets,
    _generation_rows_with_favorecido_in_legacy_column,
    _load_generation_projection_goal_by_month,
)


class GenerationGoalTests(unittest.TestCase):
    def test_2027_generation_source_favorecido_is_normalized_from_column_i(self):
        headers = [
            "Usina",
            "Status Usina",
            "UC",
            "Distribuidora",
            "Mês de referência 2027",
            "Geração Consolidada Líquida 2027",
            "Geração Projetada Líquida Travada 2027",
            "Geração Projetada Líquida Travada 2026",
            "Favorecido",
        ]
        source_row = [
            "Usina PR",
            "Ativa",
            "123456",
            "COPEL",
            "01-01-2027",
            "100",
            "2000",
            "1750",
            "Sion - Helexia PR",
        ]

        normalized = _generation_rows_with_favorecido_in_legacy_column(
            headers,
            [source_row],
        )

        self.assertEqual(normalized[0][6], "2000")
        self.assertEqual(normalized[0][7], "Sion - Helexia PR")

    @patch("src.poll._distributor_for_rateio_spreadsheet", return_value="COPEL")
    @patch("src.poll._get_worksheet_with_aliases")
    @patch("src.poll.read_all_rows")
    def test_copel_matriz_goal_includes_helexia_pr_generation(
        self,
        read_all_rows,
        get_worksheet,
        distributor_for_spreadsheet,
    ):
        del get_worksheet, distributor_for_spreadsheet
        read_all_rows.return_value = [
            ["", "", "", "", "", "", "1250", "", "8750", "", "", "", "01-02-2027"]
        ]

        goals = _load_generation_projection_goal_by_month(
            "copel-sheet",
            "Sion - Matriz",
        )

        self.assertEqual(goals, {"01-02-2027": Decimal("10000")})

    @patch("src.poll._distributor_for_rateio_spreadsheet", return_value="AmE")
    @patch("src.poll._get_worksheet_with_aliases")
    @patch("src.poll.read_all_rows")
    def test_other_distributors_keep_their_own_favorecido_goal(
        self,
        read_all_rows,
        get_worksheet,
        distributor_for_spreadsheet,
    ):
        del get_worksheet, distributor_for_spreadsheet
        read_all_rows.return_value = [
            ["", "", "", "", "", "", "1250", "", "8750", "", "", "", "01-02-2027"]
        ]

        goals = _load_generation_projection_goal_by_month(
            "ame-sheet",
            "Sion - Matriz",
        )

        self.assertEqual(goals, {"01-02-2027": Decimal("1250")})

    def test_positive_2027_generation_reaches_anchor_remainder_allocation(self):
        rateio_month = "01-02-2027"
        row = {
            "frozen": False,
            "skip_calc": False,
            "uc_keys": ["45351163"],
            "uc": "45351163",
            "uc_aneel": "169975703110",
            "month_ref": "01-01-2027",
            "rateio_month": rateio_month,
            "status": "Ativo",
            "invoice_issue_day": 1,
            "source_i": Decimal("0"),
            "source_i_has_value": True,
            "h": Decimal("100000"),
            "m_consumo": Decimal("100000"),
            "uc_ancora": True,
            "favorecido": "Sion - Helexia PR",
            "tensao": "Alta Tensão",
            "is_contingencia": False,
            "excluded_from_shared_rateio": False,
        }

        _apply_k_l_m_targets(
            [row],
            goals={rateio_month: Decimal("10000")},
            sheet_label="Sion - Matriz",
            current_month=202611,
            configured_coefficient_by_month={rateio_month: Decimal("0.10")},
            anchor_allocation_months={rateio_month},
            allocation_favorecido="Sion - Matriz",
            allocation_distributor="COPEL",
        )

        self.assertGreater(row["l_target"], Decimal("0"))
        self.assertEqual(row["m_target"], Decimal("10000"))


if __name__ == "__main__":
    unittest.main()
