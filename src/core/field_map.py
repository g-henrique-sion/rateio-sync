"""
Mapeamento de campos ClickUp para colunas da planilha.

Colunas:
A=Alteracao Rateio para mes (vazio no sync)
B=Status
C=Plano
D=UC
E=Apelido
F=Razao Social
G=Mes de Referencia
H=PREV. CONSUMO
I=Saldo
"""

FIELD_MAP = {
    "alteracao_rateio_mes": {
        "header": "Alteracao Rateio para mes",
        "source": "placeholder",
    },
    "status": {
        "header": "Status",
        "source": "custom_field",
        "cf_id": "1a5118f7-b9a0-466f-889d-37edd76bd304",
        "transform": "resolve_dropdown",
    },
    "plano": {
        "header": "Plano",
        "source": "custom_field",
        "cf_id": "0e009719-1e94-482a-825a-c359e268727e",
        "transform": "resolve_dropdown",
    },
    "uc": {
        "header": "UC",
        "source": "custom_field",
        "cf_id": "abb7e1e9-3c99-4044-b20c-5eb19575a6d5",
    },
    "apelido": {
        "header": "Apelido",
        "source": "custom_field",
        "cf_id": "44468280-77b8-4bf1-8b20-416dcc752646",
    },
    "razao_social": {
        "header": "Razao Social",
        "source": "custom_field",
        "cf_id": "dfb0de9b-121a-4bf6-977f-dfb5eec523cb",
    },
    "mes_referencia": {
        "header": "Mes de Referencia",
        "source": "invoice_field",
        "invoice_key": "nuMesReferencia",
    },
    "projecao_consumo": {
        "header": "PREV. CONSUMO",
        "source": "invoice_field",
        "invoice_key": "projecao_consumo",
    },
    "saldo_23_24": {
        "header": "Saldo",
        "source": "invoice_field",
        "invoice_key": "saldo_23_24",
    },
}

# Roteamento por distribuidora para abas especificas
TAB_ROUTING = {
    "field_id": "84bd83df-2e9f-485f-ae77-0d5c4e02ddf9",
    "options": {
        "COPEL": "12954f6f-86be-48f8-81b6-8df5b118733f",
        "Energisa MS": "d5d26875-9beb-4c62-85e7-a95d90fb8920",
        "CELESC": "d4e00593-30b8-423c-b3b6-c7a498d7d435",
        "AmE": "c19855c6-d4a7-446a-92c9-9e00f213c143",
    },
}

TAB_BY_OPTION_ID = {
    option_id: tab_name
    for tab_name, option_id in TAB_ROUTING["options"].items()
}

TARGET_SHEET_TABS = list(TAB_ROUTING["options"].keys())

# Mapa estatico de opcoes dos dropdowns (id -> nome)
DROPDOWN_OPTIONS = {
    # Status Detalhado
    "1a5118f7-b9a0-466f-889d-37edd76bd304": {
        "12a08c0a-9e2b-4ed0-b40e-7313791840eb": "Ativo",
        "d322386d-2b63-43cb-8036-cae3cf94531f": "Retirado da Usina - Saldo",
        "d8831b76-8f4d-4744-938b-82efef419437": "Retirado da Usina - Inadimplencia",
        "ae80bc03-d28f-4bc3-ae2f-653accd64e0b": "Aguardando Cadastro - Usina",
        "92cb3240-3915-43ac-a9d9-517a8903b448": "A Retirar da Usina - Demissao",
        "a74997a7-e393-4bfc-9241-ed76a0a05569": "Encerrado - Financeiro",
        "25a28dc4-16ff-4ecf-b94f-a7b3a6eef42c": "Encerrado - Troca de Plano",
        "b39e4722-25c1-4bbb-980d-dc5d43789dc3": "Aguardando saida de concorrente",
        "15f1bd8a-215f-4869-9386-fb725a7b8adb": "Cadastro em andamento",
        "265047c8-7ca1-44ec-a627-c598aab081ba": "Baixo Consumo",
        "5afbfb3f-8c96-455d-8d87-164ed477ae52": "Retirado da Usina - CR",
        "1c4aabb2-3fb0-4e2d-8a67-03025ac2654d": "Aguardando Cadastro - em Contingencia",
        "c4876bc8-67fd-4db1-8d3e-60a8995ee839": "Ativo - em Contingencia",
        "6460b3b7-e6c7-484c-ac90-6a1f9d2d0ca0": "A Retirar da Usina - CR",
        "32706ab8-e1c8-4052-ab94-3261c52acc72": "Retirado da Usina - Demissao",
        "2e7e31aa-13c8-4a78-a550-3d8d8ea6bd5a": "A Retirar da Usina - Inadimplencia",
        "2ff02b08-cd28-48b0-8ab4-b516ed8be73d": "Eliminado",
        "a2ff017f-77d1-403e-ba35-c375057144d0": "Excluido",
        "a858ffec-5fe1-44ac-84aa-da5ead59ce7b": "Demitido",
        "3d472363-6bfb-4b0f-a7b8-d8f8e850a79e": "Aguardando Troca de Titularidade",
        "29e28b58-2922-49c9-a8d0-f2a83d398d0a": "Planejamento - Black",
        "633b62b9-1c73-4de6-bab3-c78410ac80c5": "A Retirar da Usina - Black",
        "9d26bcc2-174b-487a-b7bb-46708b3ebf58": "Retirado da Usina - Black",
        "c5807601-bde8-4a50-8af7-4f5453dbfc74": "A Retirar da Usina - Saldo",
    },
    # Plano de Adesao
    "0e009719-1e94-482a-825a-c359e268727e": {},
    # Distribuidora (aba de destino)
    TAB_ROUTING["field_id"]: TAB_BY_OPTION_ID,
}

COLUMN_ORDER = [
    "alteracao_rateio_mes",
    "status",
    "plano",
    "uc",
    "apelido",
    "razao_social",
    "mes_referencia",
    "projecao_consumo",
    "saldo_23_24",
]

# Colunas J/K/L/M sao tratadas no poll.py apos escrita base:
# - K: calculada (I + J - H, piso em 0)
# - L: coeficiente mensal (contingencia fixo e demais ajustados por meta)
# - M: novo rateio (previsao da UC no mes de alteracao * L - K)
# - N: dia de emissao da fatura da distribuidora
# - O: favorecido usado no roteamento da linha
# - P: UC Aneel


def get_headers() -> list[str]:
    """Retorna lista de headers na ordem correta."""
    return [FIELD_MAP[k]["header"] for k in COLUMN_ORDER]
