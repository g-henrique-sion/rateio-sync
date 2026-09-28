# Funcionamento das planilhas de rateio

Este documento descreve o funcionamento atual do projeto `rateio_sync`, com foco em contexto tecnico para outra IA ou outro desenvolvedor. O arquivo principal do sync e `src/poll.py`; os mapeamentos de campos ficam em `src/core/field_map.py` e `src/core/row_builder.py`.

Regra de seguranca operacional: nao executar full sync, delta sync, scripts com `--apply`, ou qualquer escrita em Google Sheets/ClickUp sem autorizacao explicita. A maior parte dos scripts utilitarios simula por padrao, mas alguns exportadores escrevem por padrao e exigem `--dry-run` para apenas consultar.

## Visao geral

O sistema sincroniza dados de ClickUp, PowerRev e Google Sheets para manter planilhas de rateio por distribuidora. Existem dois ciclos principais:

| Ciclo | Funcao | Comportamento |
| --- | --- | --- |
| Full sync | `full_sync()` | Reconstrui as abas abertas do rateio, preserva meses fechados/congelados, recalcula saldos/coefs/rateio, atualiza Geracao Total, Configuracao e Formularios. |
| Delta sync | `delta_sync(last_updated_ts)` | Atualiza ou limpa linhas ja existentes para tasks alteradas no ClickUp. Nao adiciona task nova ao rateio principal; task nova entra no proximo full sync. |

O processo (`python -m src.poll`) sempre executa um full sync inicial completo antes de iniciar deltas. Depois agenda full sync diario as 03:00 no timezone `APP_TIMEZONE` e executa delta a cada `DELTA_SYNC_INTERVAL_S` segundos.

## Planilhas principais

| Uso | ID | Link | Observacao |
| --- | --- | --- | --- |
| COPEL | `11TW3jDv8bZYJxPA2sOx73NUdY8QiMbFsgnrfeEopOww` | https://docs.google.com/spreadsheets/d/11TW3jDv8bZYJxPA2sOx73NUdY8QiMbFsgnrfeEopOww/edit | Rateio ativo em `Sion - Matriz`; aba `Helexia PR` preservada como historico fechado. |
| AmE | `1VK322_aF3N6_JXpvNX2dFEKOn4QKVB9Rz3lZRj7FpOk` | https://docs.google.com/spreadsheets/d/1VK322_aF3N6_JXpvNX2dFEKOn4QKVB9Rz3lZRj7FpOk/edit | Rateio em Sion - Matriz. |
| Energisa MS | `1usAlgI5WiwLT1aOIy7-yy68EIJ3vFYvcnFwaf22ryyg` | https://docs.google.com/spreadsheets/d/1usAlgI5WiwLT1aOIy7-yy68EIJ3vFYvcnFwaf22ryyg/edit | Rateio ativo: `Sion - Helexia MS`. |
| CELESC | `1f3ljN863TAg1joLOnyhoG9Fh125LdIherxpArrxxTsk` | https://docs.google.com/spreadsheets/d/1f3ljN863TAg1joLOnyhoG9Fh125LdIherxpArrxxTsk/edit | Rateio em Sion - Matriz. |
| Projecoes | `1flNyO53loY__fwO-TDqffAFAOdw7KuyjVM9cYGZmRSI` | https://docs.google.com/spreadsheets/d/1flNyO53loY__fwO-TDqffAFAOdw7KuyjVM9cYGZmRSI/edit | Fonte externa das abas `Projecao de Consumo` e `Projecao de Geracao`. |
| Pesquisa / Nova UC | `1nhw59fViA8psrshFt7zpqMMZGVYFWnfndPk0AAzGB7E` | https://docs.google.com/spreadsheets/d/1nhw59fViA8psrshFt7zpqMMZGVYFWnfndPk0AAzGB7E/edit | Usada por scripts utilitarios de pesquisa e atualizacao de UC Aneel. |

O gerenciador de Sheets usa `HEADER_ROWS = 1`; portanto, a linha 1 e tratada como cabecalho e os dados gerenciados comecam na linha 2. Comentarios antigos no codigo/README podem mencionar duas linhas preservadas, mas a regra efetiva do codigo atual e uma linha de cabecalho.

## Fontes de dados

### ClickUp

Endpoint base: `https://api.clickup.com/api/v2`.

O sync principal busca tasks em:

| Lista | Uso |
| --- | --- |
| `901322296001` | Lista principal configurada em `CLICKUP_LIST_IDS`. |
| `901321549851` | Lista principal configurada em `CLICKUP_LIST_IDS`. |

A chamada e paginada com:

```text
GET /list/{list_id}/task?page={page}&limit={limit}&include_closed=true&subtasks=true
```

No delta, tambem envia `date_updated_gt={timestamp}`. O sync usa `include_closed=true` para enxergar tasks fechadas e depois aplica os filtros de status no proprio codigo.

Campos ClickUp usados pelo rateio principal:

| Dado | Campo ClickUp |
| --- | --- |
| Status detalhado | `1a5118f7-b9a0-466f-889d-37edd76bd304` |
| Plano | `0e009719-1e94-482a-825a-c359e268727e` |
| UC antiga | `abb7e1e9-3c99-4044-b20c-5eb19575a6d5` |
| Apelido | `44468280-77b8-4bf1-8b20-416dcc752646` |
| Razao Social | `dfb0de9b-121a-4bf6-977f-dfb5eec523cb` |
| Distribuidora / rota | `84bd83df-2e9f-485f-ae77-0d5c4e02ddf9` |
| Favorecido | `0a73e7b8-febe-4982-9263-06efd75612e1` |
| Dia de emissao da fatura | `c4f18991-f556-4019-af84-157c55aada63` |
| UC Aneel | `cd8687a7-0393-45b9-8292-f9b878b31512` |

Opcoes da distribuidora:

| Distribuidora | Option ID |
| --- | --- |
| COPEL | `12954f6f-86be-48f8-81b6-8df5b118733f` |
| Energisa MS | `d5d26875-9beb-4c62-85e7-a95d90fb8920` |
| CELESC | `d4e00593-30b8-423c-b3b6-c7a498d7d435` |
| AmE | `c19855c6-d4a7-446a-92c9-9e00f213c143` |

### PowerRev

Endpoint base padrao: `https://api.powerrev.com.br:3400`.

Fluxo usado:

| Passo | Endpoint | Uso |
| --- | --- | --- |
| Autenticacao | `POST /sign` | Envia `accountId` e `apiKey`, recebe token. Se `POWERREV_TOKEN` existir, usa token estatico. |
| Consumer units | `GET /consumer-unit` | Fallback para resolver UC quando a fatura nao traz `nuInstalacao`/`cdInstalacao`. |
| Lista de faturas | `GET /invoice?nuAnoMes=YYYYMM` | Busca faturas do mes de referencia. |
| Detalhe em lote | `POST /invoice/batch-export` e `GET /invoice/batch-export/{jobId}` | Caminho preferencial para buscar detalhes de muitas faturas sem fazer uma request por linha da planilha. |
| Detalhe por fatura | `GET /invoice/{idFaturaConsumo}` | Fallback quando o batch-export nao retorna algum detalhe. |

Para cada UC e mes, quando ha mais de uma fatura, o codigo escolhe a mais recente por `dtEmissao` e depois por `invoiceId`.

O saldo de energia usado no rateio soma os itens do detalhe da fatura com IDs:

| ID PowerRev | Significado conhecido |
| --- | --- |
| `23` | Saldo de Energia HFP kWh |
| `24` | Saldo de Energia HP kWh |
| `626` | Saldo de Energia TP kWh |

O resultado e exposto internamente como `saldo_23_24`, apesar de hoje incluir tambem o item `626`. O cache persistido fica em `.powerrev_invoice_detail_cache.json`, com versao de cache `3`, incluindo `dtProximaLeitura`.

### Planilha de Projecoes

A planilha externa de projecoes alimenta duas coisas diferentes:

| Aba | Uso |
| --- | --- |
| `Projecao de Consumo` | Fonte da coluna H do rateio e tambem base alternativa para a coluna M no mes de alteracao. |
| `Projecao de Geracao` | Fonte da aba `Geracao Total`, que por sua vez define a meta mensal usada para calcular coeficiente e novo rateio. |

Na `Projecao de Consumo`, o indice e `(UC normalizada, Mes de referencia)`. O codigo le:

| Coluna | Uso |
| --- | --- |
| C | UC |
| F | Mes de referencia |
| G | Valor preferencial de consumo |
| H | Fallback se G estiver vazio |

Se nao houver previsao de consumo para a UC e mes, a linha daquele mes nao entra no rateio principal.

## Abas de rateio principal

As abas de saida do rateio sao organizadas por distribuidora e grupo de rateio:

| Distribuidora | Favorecidos habilitados | Aba de saida |
| --- | --- | --- |
| COPEL | `Sion - Matriz`, `Sion - Helexia PR` | Ambos na aba `Sion - Matriz` |
| Energisa MS | `Sion - Matriz`, `Sion - Helexia MS` | `Sion - Matriz`, `Helexia MS` |
| CELESC | `Sion - Matriz` | `Sion - Matriz` |
| AmE | `Sion - Matriz` | `Sion - Matriz` |

O rateio de `Sion - Matriz` segue ativo. A antiga regra do campo `Rateio na Sion - Matriz` ligada a `Sobra para Favorecido` foi removida. Na COPEL, cooperados `Sion - Helexia PR` recebem energia pelo mesmo calculo e meta mensal da Matriz na aba `Sion - Matriz`. A aba `Helexia PR` conserva seu historico fechado e deixa de receber linhas novas.

### Janela de meses

O full sync gera uma janela de 7 meses de referencia: 3 meses anteriores, mes atual e 3 meses futuros. A coluna A, `Alteracao Rateio para mes`, sempre recebe o mes seguinte ao mes de referencia da coluna G.

Exemplo: se G for `01-08-2026`, A sera `01-09-2026`.

Para PowerRev, o codigo busca uma fatura a mais antes do primeiro mes da janela, porque a coluna I de um mes usa o saldo final da fatura do mes anterior. Como faturas futuras normalmente nao existem, a busca de PowerRev vai ate o mes atual.

### Colunas A:R

| Coluna | Nome | Como e formada |
| --- | --- | --- |
| A | Alteracao Rateio para mes | Mes da coluna G + 1 mes. |
| B | Status | ClickUp `1a5118f7-b9a0-466f-889d-37edd76bd304`, resolvendo dropdown para label. |
| C | Plano | ClickUp `0e009719-1e94-482a-825a-c359e268727e`, resolvendo dropdown para label. |
| D | UC | ClickUp UC antiga `abb7e1e9-3c99-4044-b20c-5eb19575a6d5`; se vazia, fallback para UC Aneel `cd8687a7-0393-45b9-8292-f9b878b31512`. |
| E | Apelido | ClickUp `44468280-77b8-4bf1-8b20-416dcc752646`. |
| F | Razao Social | ClickUp `dfb0de9b-121a-4bf6-977f-dfb5eec523cb`. |
| G | Mes de Referencia | Mes da janela, formatado como `01-MM-YYYY`. |
| H | PREV. CONSUMO | Projecao de consumo da planilha externa, por UC e mes G. Usa coluna G da origem e fallback para H da origem. |
| I | Saldo | Saldo PowerRev da fatura do mes anterior ao mes G. Soma itens 23, 24 e 626. Se nao houver saldo PowerRev, pode reutilizar a coluna K do mes anterior da propria aba. |
| J | Ultimo Rateio | Historico da aba `Rateio` da mesma distribuidora, conforme regra de dia de emissao. Se nao houver historico, pode reutilizar a coluna M ja calculada para o mes historico. |
| K | Saldo final calculado | `max(I + J - H, 0)`. |
| L | Coeficiente | Coeficiente mensal calculado por meta de geracao, manual da Configuracao, contingencia ou alocacao especial. |
| M | Novo Rateio | `max(previsao_do_mes_de_alteracao * L - K, 0)`, arredondado para inteiro. Se nao existir previsao para o mes A, usa H. |
| N | Dia de emissao da fatura da distribuidora | Usa o dia da ultima `dtProximaLeitura` conhecida na PowerRev; fallback para ClickUp `c4f18991-f556-4019-af84-157c55aada63`. |
| O | Favorecido | Favorecido original da task. Na COPEL, Helexia PR continua escrito como `Sion - Helexia PR` na aba `Sion - Matriz`. |
| P | UC Aneel | ClickUp `cd8687a7-0393-45b9-8292-f9b878b31512`; se vazio, escreve `Sem UC Aneel`. |
| Q | Tensão | Grupo A ou B conforme o campo `Produto` da ClickUp. |
| R | UC Âncora | Checkbox `UC Ancora` da ClickUp. |

### Match de UC

O codigo usa candidatos ordenados para match:

```text
1. UC antiga: abb7e1e9-3c99-4044-b20c-5eb19575a6d5
2. UC Aneel: cd8687a7-0393-45b9-8292-f9b878b31512
```

Essa lista e usada para consultas de PowerRev, projecao de consumo e localizacao de linhas existentes no delta. A normalizacao principal do rateio remove hifen e espacos de borda; alguns scripts utilitarios usam normalizacao mais agressiva, mantendo apenas numeros.

### Filtros que impedem a linha de aparecer

Uma task nao aparece no rateio principal se cair em qualquer uma destas condicoes:

| Condicao | Efeito |
| --- | --- |
| Sem UC antiga e sem UC Aneel | Ignorada antes de qualquer roteamento. |
| Distribuidora sem rota valida | Ignorada. |
| Favorecido vazio ou nao suportado para a distribuidora | Ignorada. |
| Plano normalizado igual a `sem faturamento` | Ignorada. |
| Status excluido do rateio | Ignorada. |
| Status `Novo Cooperado` ainda dentro da carencia por distribuidora | Ignorada para aqueles meses. |
| Sem previsao de consumo para UC+mes | Ignorada naquele mes. |
| Task nova chegou so no delta | Nao e adicionada ate o proximo full sync. |
| Mes fechado/congelado na `Configuracao` | Linha existente e preservada; linha aberta nao substitui esse mes. |

Status excluidos do rateio incluem, entre outros: `Cancelado - Autoconsumo`, `Planejamento - Black`, `Aguardando Cadastro`, `Aguardando Cadastro - Usina`, `Demitido`, `Excluido`, `Encerrado - Financeiro`, `Encerrado - Troca de Plano`, status de retirada/demissao/inadimplencia/black e `A Encerrar - Financeiro`. Qualquer status normalizado que comece com `aguardando cadastro ` tambem e excluido.

Ha tambem status excluidos apenas da parte de projecao/calculo, que limpam H:I/J/K/L/M quando aplicavel: `Cancelado - Autoconsumo`, `Encerrado - Financeiro`, `Baixo Consumo`, status de retirada, `A Retirar da Usina - Black` e `A Encerrar - Financeiro`.

### Isolamento COPEL / MONTE SIAO

Na COPEL, linhas com Razao Social `MONTE SIAO COOPERATIVA DE ENERGIA` continuam aparecendo normalmente nas abas de rateio, mas ficam fora do rateio compartilhado. Elas nao entram na validacao de meta de geracao, no calculo do coeficiente global, no bloco de contingencia, na alocacao especial de sobra, na soma de `Novo Rateio` nem na diferenca mensal.

Essas linhas ainda podem ter calculos proprios por UC, como saldo inicial, ultimo rateio historico e saldo final. O sync zera o impacto compartilhado deixando `Coeficiente` e `Novo Rateio` vazios para elas.

### Novo Cooperado

Status tratados como novo cooperado:

```text
Novo Cooperado
Novo Cooperado - Em Contingencia, com ou sem acento
```

A carencia e baseada no mes em que o sync esta rodando, nao na data em que a task virou novo cooperado.

| Distribuidora | Mes minimo de alteracao |
| --- | --- |
| COPEL | Mes atual + 2 meses |
| AmE | Mes atual + 1 mes |
| CELESC | Mes atual + 1 mes |
| Energisa MS | Mes atual + 1 mes |

Exemplo: se o sync roda em agosto/2026, um novo cooperado COPEL so aparece a partir de `01-10-2026`; AmE, CELESC e Energisa MS aparecem a partir de `01-09-2026`.

### Duplicidade por UC

Antes de montar as linhas, o full sync prioriza uma unica task por UC. Status de baixa prioridade perdem para status ativos/elegiveis. Se duas tasks da mesma UC continuam comparaveis, a task com `date_updated` mais recente ganha.

Isso significa que uma task individual pode nao aparecer porque outra task da mesma UC foi escolhida como representante.

## Calculo de I, J, K, L e M

O recalculo de I:M acontece depois que as linhas base A:P sao escritas ou atualizadas.

### Coluna I: saldo

Para a linha de referencia G, o saldo PowerRev vem da fatura do mes anterior. Exemplo: linha G `01-07-2026` procura saldo na fatura `202606`.

Se a PowerRev traz saldo, I recebe esse saldo, truncado/formatado como numero de planilha. Se nao traz saldo, o algoritmo tenta reutilizar a coluna K do mes anterior da mesma UC. Se nao houver K anterior, I fica vazio e o calculo considera zero.

### Coluna J: ultimo rateio

O mes historico usado em J depende do dia de emissao:

| Regra | Mes historico |
| --- | --- |
| Dia de emissao <= limite | A - 2 meses |
| Dia de emissao > limite ou sem dia | A - 1 mes |

Limite padrao: dia `10`. Para AmE: dia `7`.

A origem do historico e a aba `Rateio` da mesma planilha/distribuidora:

| Distribuidora | Range lido | UC | Mes de alteracao | Valor historico |
| --- | --- | --- | --- | --- |
| COPEL | `D2:I` | D | H | I |
| Demais | `C2:J` | C | H | J |

Se o historico nao tem valor para `(UC, mes historico)`, o codigo tenta usar a coluna M ja calculada para aquela UC e mes historico dentro da propria aba. Se tambem nao existir, J fica vazio e o calculo usa zero.

### Coluna K: saldo final

Formula logica:

```text
K = max(I + J - H, 0)
```

Se a linha estiver vazia, invalida, congelada ou com status que deve limpar projecao/calculo, K pode ficar preservada ou vazia conforme o caso.

### Coluna L: coeficiente

O coeficiente e resolvido por mes de alteracao A e por aba/favorecido. A prioridade efetiva e:

```text
1. Linhas de contingencia usam coeficiente fixo/configurado de contingencia.
2. Se houver alocacao especial valida, ela manda sobre o coeficiente manual.
3. Se nao houver alocacao especial, coeficiente manual da Configuracao pode ser aplicado.
4. Se nao houver manual, o codigo calcula o maior coeficiente que nao ultrapassa a meta mensal de geracao.
```

Uma linha e considerada contingencia quando o status contem `contingencia`, ignorando acentos e caixa.

Coeficiente de contingencia padrao:

| Caso | Coeficiente |
| --- | --- |
| AmE | `1.00000000` |
| CELESC | `1.00000000` |
| Demais casos | `0.90000000` |

Energisa MS / Sion - Helexia MS pode ter coeficiente de contingencia configurado na aba `Configuracao`.

### Coluna M: novo rateio

Formula logica por linha:

```text
M = max(previsao_consumo_do_mes_A * L - K, 0)
```

A previsao usada em M tenta buscar `(UC, mes A)` na `Projecao de Consumo`. Se nao existir, usa H, que e a previsao do mes G. O valor final de M e arredondado para inteiro.

### Meta mensal de geracao

A meta do calculo vem da aba `Geracao Total` da planilha da distribuidora. Para cada favorecido, o codigo usa a coluna de geracao projetada, nao a consolidada:

| Favorecido | Coluna projetada usada como meta | Coluna consolidada informativa |
| --- | --- | --- |
| Sion - Matriz | G | H |
| Sion - Helexia PR | I | J |
| Sion - Helexia MS | K | L |

O mes da meta e lido na coluna M da aba `Geracao Total`.

Antes de escrever o rateio, o full sync confere as metas de geracao dos meses necessarios. Se faltar meta para algum mes aberto, registra um aviso e deixa L/M vazios nesse mes.

## Aba Configuracao

A aba `Configuracao` existe em cada planilha de distribuidora. O full sync preserva o layout da COPEL e padroniza AmE, Energisa MS e CELESC, conservando os valores manuais por nome de coluna e os meses fechados.

Cabecalhos possiveis:

| Coluna logica | Uso |
| --- | --- |
| `Alteracao Rateio para o mes` | Mes A que a configuracao controla. |
| `Coeficiente <Favorecido>` | Coeficiente manual por favorecido. Formulas nao sao consideradas configuracao manual. |
| `Status` | Se for `Fechado`, congela o mes. Qualquer outro valor, inclusive vazio ou desconhecido, e tratado como aberto. |
| `Coeficiente Contingencia <Favorecido>` | Coeficiente das linhas em contingencia. |
| `Coeficiente Baixa Tensao <Favorecido>` | Coeficiente manual para cooperados de baixa tensao. |
| `Coeficiente Alta Tensao <Favorecido>` | Coeficiente manual para cooperados de alta tensao. |

Nas distribuidoras fora da COPEL, a contingencia prevalece sobre a tensao. Para os demais cooperados, o coeficiente de tensao preenchido prevalece sobre o coeficiente geral do favorecido. Quando ha coeficiente manual aplicavel e UCs com `UC Ancora` marcada na ClickUp, o restante da geracao mensal e dividido igualmente entre as ancoras elegiveis da mesma distribuidora e favorecido de rateio. Na COPEL, `Sion - Helexia PR` pertence ao grupo de rateio de `Sion - Matriz` e pode servir de ancora desse grupo. Sem coeficiente manual, segue o calculo automatico. Sem ancora elegivel, os coeficientes manuais continuam aplicados e a sobra fica sem destino; o log registra o caso. O checkbox fica visivel na coluna R da aba de rateio.

A tensao usa o campo `Produto` da ClickUp: a opcao `Cooperativa Grupo A` e alta tensao; as opcoes de Grupo B sao baixa tensao.

As colunas de configuracao de `Sion - Matriz` alimentam o rateio normal nas quatro distribuidoras.

## Aba Geracao Total

A aba `Geracao Total` e recriada no full e tambem em todo delta, mesmo quando nao ha tasks alteradas no ClickUp. A fonte e a aba `Projecao de Geracao` da planilha externa.

Colunas lidas da origem:

| Coluna origem | Uso |
| --- | --- |
| A | Usina |
| B | Status da usina |
| C | UC |
| D | Distribuidora |
| E | Mes de referencia |
| F | Geracao consolidada |
| G | Geracao projetada |
| H | Favorecido |

Linhas com distribuidora nao mapeada sao ignoradas. Se o Favorecido estiver vazio ou nao suportado:

| Caso | Comportamento |
| --- | --- |
| Distribuidora com um unico favorecido habilitado, como AmE/CELESC | Assume esse favorecido. |
| Distribuidora com mais de um favorecido, como COPEL/Energisa MS | A linha pode aparecer em A:F, mas fica fora dos totais mensais G:L. |

Colunas de saida:

| Coluna destino | Conteudo |
| --- | --- |
| A | Usina |
| B | Status da usina |
| C | UC |
| D | Mes de referencia |
| E | Geracao projetada da linha |
| F | Geracao consolidada da linha |
| G/H | Total mensal projetado/consolidado de Sion - Matriz |
| I/J | Total mensal projetado/consolidado de Sion - Helexia PR |
| K/L | Total mensal projetado/consolidado de Sion - Helexia MS |
| M | Mes de referencia do total |

Os totais mensais aparecem uma vez por mes, na primeira linha daquele mes apos a ordenacao por usina, mes e UC.

## Formularios

As abas de formulario sao sincronizadas no full e no delta:

| Distribuidora | Aba |
| --- | --- |
| COPEL | `Formulario COPEL` |
| AmE | `Formulario AmE` |
| CELESC | `Formulario CELESC` |
| Energisa MS | `Formulario Energisa MS` |

A origem de cada formulario e a aba historica de rateio da propria planilha:

| Distribuidora | Aba fonte |
| --- | --- |
| COPEL | `RATEIO_COPEL_HISTORY_TAB`, padrao `Rateio` |
| Demais | `RATEIO_HISTORY_TAB`, padrao `Rateio` |

O codigo le `A1:K` e localiza as colunas por cabecalho, nao por posicao fixa. Aliases aceitos:

| Campo logico | Cabecalhos aceitos |
| --- | --- |
| Usina | `USINA` |
| Razao Social | `RAZAO SOCIAL` |
| UC | `UC` |
| Nova UC / UC Aneel | `NOVA UC`, `UC ANEEL` |
| Percentual | `%`, `PERCENTUAL`, `PORCENTAGEM` |
| Alteracao | `ALTERACAO PARA MES`, `ALTERACAO PARA O MES`, `ALTERACAO RATEIO PARA O MES` |

Colunas escritas atualmente em todos os formularios:

| Coluna | Conteudo | Fonte |
| --- | --- | --- |
| A | Razao Social | ClickUp `dfb0de9b-121a-4bf6-977f-dfb5eec523cb`; fallback para Rateio. |
| B | CPF/CNPJ | ClickUp `6bcbff0f-3228-44e1-b7d7-14efa915fc31`. |
| C | UC | Aba Rateio fonte. |
| D | Percentual | Aba Rateio fonte. |
| E | UC da usina/projeto | Tasks de projeto por nome da usina. |
| F | Usina | Aba Rateio fonte. |
| G | Alteracao | Data da aba Rateio fonte, formatada como `DD/MM/YYYY`. |
| H | UC Aneel do cooperado | ClickUp `cd8687a7-0393-45b9-8292-f9b878b31512`; fallback para `NOVA UC`/`UC ANEEL` da origem. |
| I | Endereco | Campos de endereco do ClickUp, formatados. |
| J | UC Aneel da usina/projeto | Tasks de projeto por nome da usina. |

Tasks de projeto usadas nos formularios:

| Lista | Uso |
| --- | --- |
| `901304117744` | Cards de usina/projeto para recuperar UC e UC Aneel da usina. |
| `901327022900` | Cards de usina/projeto para recuperar UC e UC Aneel da usina. |

Para casar a usina do Rateio com a task de projeto, o nome da task de projeto e normalizado removendo sufixo no padrao ` - UC ...`. Exemplo: `UFV Auth II - UC 11474025` vira `UFV Auth II`.

Campos de endereco:

| Parte | Campo ClickUp |
| --- | --- |
| Rua | `26d33756-428f-4f28-a1b4-485cb875429e` |
| Numero | `6348124b-60d4-40d5-80fe-86019c173d4e` |
| Complemento | `6257313d-18da-4ebb-aa03-d07846b5da8d` |
| Bairro | `a9ce9f60-cf2b-4eb8-975c-2fe8c7c27591` |
| Cidade | `81ba9425-6386-40ea-8a92-e270e8284bd7` |
| Estado | `06361a45-790b-4fbe-9e66-427cfb28e7ec` |
| CEP | `f24d971c-4037-41a7-a1f1-f92c84569993` |

Formato:

```text
Rua, numero, complemento - bairro - cidade-estado CEP
```

Dropdowns, incluindo Estado, sao resolvidos por `type_config.options` do ClickUp quando disponivel; isso converte codigo interno em labels como `AM`, `SC` ou `MS`.

## Full sync detalhado

Fluxo simplificado de `full_sync()`:

1. Busca todas as tasks das listas principais do ClickUp com `include_closed=true`.
2. Guarda `_known_task_ids`, usado depois para saber se uma task no delta e nova.
3. Deduplica/prioriza tasks por UC.
4. Monta indices da PowerRev para a janela necessaria.
5. Carrega indice de `Projecao de Consumo`.
6. Carrega historico de ultimo rateio por distribuidora.
7. Gera linhas por distribuidora/favorecido aplicando filtros, carencia de novo cooperado, rota e projecao.
8. Sincroniza `Geracao Total`.
9. Le meses fechados/congelados da `Configuracao`.
10. Valida metas de geracao para todos os meses abertos que serao calculados.
11. Escreve abas de rateio, preservando meses congelados.
12. Recalcula I:M em cada aba.
13. Atualiza layout da `Configuracao`.
14. Sincroniza todos os formularios.

Se qualquer excecao acontecer no full sync inicial ou programado, `_run_full_sync_until_success()` reinicia sessoes e tenta o full inteiro novamente. O log diz que nenhuma execucao parcial sera aceita, mas se uma falha ocorrer depois de alguma escrita em Sheets, a planilha pode ter ficado parcialmente atualizada ate a proxima tentativa completar.

## Delta sync detalhado

Fluxo simplificado de `delta_sync(last_updated_ts)`:

1. Sincroniza `Geracao Total` sempre.
2. Busca tasks alteradas desde `last_updated_ts`.
3. Se nao houver tasks, sincroniza formularios e encerra.
4. Separa tasks conhecidas de tasks novas.
5. Para tasks conhecidas, recalcula somente linhas existentes localizadas por `(UC antiga ou UC Aneel, mes G)`.
6. Se a task mudou de rota/status/plano/projecao/carencia, a linha antiga pode ser limpa.
7. Recalcula I:M apenas nos meses impactados.
8. Tasks novas nao sao adicionadas ao rateio principal no delta; apenas entram no proximo full sync.
9. Sincroniza formularios ao final.

O delta tambem respeita meses fechados/congelados. Se a linha existente pertence a um mes congelado, ela nao e tocada.

## Scripts utilitarios

### `src/utils/exportar_configuracao_dia_emissao_copel.py`

Cria/atualiza a aba `Configuracao Dia de Emissao` na planilha COPEL.

| Coluna | Conteudo |
| --- | --- |
| A | UC |
| B | UC Aneel |
| C | Razao Social |
| D | Dia de emissao (ultimo) |

Busca todas as tasks do ClickUp das listas principais, sem deduplicar. Nao escreve linhas sem UC e sem UC Aneel. O dia vem da PowerRev procurando o ultimo dia conhecido no intervalo recente, padrao 3 meses para tras.

Atencao: escreve por padrao. Use `--dry-run` para apenas simular.

### `src/utils/atualizar_dia_emissao_clickup.py`

Le a aba COPEL `Configuracao Dia de Emissao` e atualiza o campo ClickUp de dia de emissao:

```text
c4f18991-f556-4019-af84-157c55aada63
```

Regras:

| Regra | Comportamento |
| --- | --- |
| Leitura | `A2:D`, usando A=UC e D=dia. |
| Dia invalido/vazio | Ignora. |
| Duplicidade mesma UC mesmo dia | Ignora duplicata. |
| Duplicidade mesma UC com dias diferentes | Ignora a UC inteira por conflito. |
| Match | Toda task ClickUp com mesma UC e atualizada, sem deduplicar por UC. |
| Escrita | `POST /task/{task_id}/field/{field_id}` com `{"value": numero}`. |

Simula por padrao. Use `--apply` para escrever.

### `src/utils/exportar_pesquisa_clickup.py`

Exporta campos do ClickUp para a planilha `1nhw59fViA8psrshFt7zpqMMZGVYFWnfndPk0AAzGB7E`, aba `PESQUISA`, colunas A:B.

Listas consultadas:

```text
901324383232
901324946417
901323337719
901322296001
901321549851
```

Campos exportados:

| Coluna | Campo |
| --- | --- |
| A | UC antiga `abb7e1e9-3c99-4044-b20c-5eb19575a6d5` |
| B | UC Aneel `cd8687a7-0393-45b9-8292-f9b878b31512` |

Atencao: escreve por padrao. Use `--dry-run` para apenas simular.

### `src/utils/atualizar_nova_uc_clickup.py`

Le a planilha `1nhw59fViA8psrshFt7zpqMMZGVYFWnfndPk0AAzGB7E`, aba `NOVA UC`, colunas A:B, e atualiza UC Aneel no ClickUp.

Regras:

| Item | Comportamento |
| --- | --- |
| Match | Coluna A normalizada contra UC antiga `abb7e1e9-3c99-4044-b20c-5eb19575a6d5`. |
| Valor destino | Coluna B normalizada para manter somente digitos. |
| Padding | Se a coluna B tiver menos de 15 digitos, adiciona zeros a esquerda ate 15 caracteres. |
| Linhas invalidas | Coluna A vazia ou coluna B sem digitos nao atualiza nada. |
| Protecao | So atualiza se o campo UC Aneel atual no ClickUp estiver vazio. |
| Campo atualizado | `cd8687a7-0393-45b9-8292-f9b878b31512`. |
| Listas | As listas do `exportar_pesquisa_clickup.py` mais `901304117744`. |

Simula por padrao. Use `--apply` para escrever.

### `src/utils/preencher_favorecido_projecao_geracao.py`

Preenche somente `Projecao de Geracao!H` com o Favorecido vindo do ClickUp.

| Item | Comportamento |
| --- | --- |
| Lista ClickUp | `901304117744` |
| Match | Google Sheets coluna C = UC contra UC da task. |
| Valor escrito | Favorecido ClickUp `0a73e7b8-febe-4982-9263-06efd75612e1`. |
| Sem match | Escreve `Sem match de UC`. |
| Match sem favorecido | Escreve `Sem favorecido configurado`. |
| Protecao | Antes de escrever, valida que H1 e `Favorecido`. |

Simula por padrao. Use `--apply` para escrever.

### `src/utils/atualizar_base_pagamento_clickup.py`

Le `src/utils/Base de Pagamento.xlsx`, aba padrao `Plan1`, e atualiza campos de tasks da lista `901304117744`.

Match:

| Prioridade | Regra |
| --- | --- |
| 1 | UC antiga ClickUp `abb7e1e9-3c99-4044-b20c-5eb19575a6d5` contra coluna C do XLSX. |
| 2 | Nome base da task contra coluna B do XLSX. Exemplo: `UFV Auth II - UC 11474025` vira `UFV Auth II`. |

Campos atualizados:

| Coluna XLSX | Campo ClickUp |
| --- | --- |
| D | `55bac4f4-8e86-4e3d-a222-c2a50baa4a74` |
| E | `26872920-65ed-4345-bd67-1513299f11a3` |
| G | `160146f2-7986-4a27-bc18-66e1c6fc3355` |

Simula por padrao. Use `--apply` para escrever.

### Backfills pontuais

| Script | Uso |
| --- | --- |
| `src/utils/backfill_coluna_j_analise_2026.py` | Preenche coluna J do rateio a partir de uma planilha externa de analise 2026. Origem padrao `1z7M1XkEWIZeDgXIwDEv_HcqeMg2zHlDOOwe_V2uEjsM`; match por UC e mes. |
| `src/utils/backfill_novo_rateio_history.py` | Backfill historico de Novo Rateio a partir da aba externa `novo rateio`; match por nome e mes. |

Esses scripts sao utilitarios pontuais e separados do fluxo normal de full/delta.

## Variaveis de ambiente importantes

| Variavel | Uso |
| --- | --- |
| `CLICKUP_TOKEN` | Token ClickUp. |
| `CLICKUP_TEAM_ID` | Time ClickUp, padrao `9013290037`. |
| `CLICKUP_PAGE_LIMIT` | Tamanho da pagina ClickUp, padrao `100`. |
| `CLICKUP_PAGE_PAUSE_S` | Pausa entre paginas ClickUp. |
| `CLICKUP_MAX_RETRIES` | Tentativas ClickUp. |
| `POWERREV_BASE_URL` | Base PowerRev, padrao `https://api.powerrev.com.br:3400`. |
| `POWERREV_AUTH_URL` | Base de autenticacao, padrao igual a `POWERREV_BASE_URL`. |
| `POWERREV_ACCOUNT_ID` | Credencial PowerRev. |
| `POWERREV_API_KEY` | Credencial PowerRev. |
| `POWERREV_TOKEN` | Token estatico opcional. |
| `POWERREV_USE_BATCH_EXPORT` | Liga/desliga batch export, padrao ligado. |
| `POWERREV_BATCH_EXPORT_IDS_PER_JOB` | Tamanho do lote de batch export, padrao `400`. |
| `POWERREV_DETAIL_CACHE_FILE` | Arquivo de cache de detalhes PowerRev. |
| `APP_TIMEZONE` | Timezone de agendamento, padrao `America/Sao_Paulo`. |
| `GOOGLE_CREDENTIALS_JSON` | Credenciais Service Account em JSON. |
| `GOOGLE_CREDENTIALS_FILE` | Arquivo local de credenciais, padrao `credentials.json`. |
| `RATEIO_*_SPREADSHEET_ID` | Sobrescreve IDs das planilhas por distribuidora. |
| `PROJECTION_SPREADSHEET_ID` | Sobrescreve planilha externa de projecoes. |
| `FULL_SYNC_INTERVAL_S` | Mantido com minimo de 86400s, mas o codigo tambem agenda full diario as 03:00. |
| `DELTA_SYNC_INTERVAL_S` | Intervalo de delta, padrao `600`. |
| `CHUNK_SIZE` | Tamanho dos lotes de escrita Sheets, padrao `300`. |
| `CHUNK_PAUSE_S` | Pausa entre chunks Sheets, padrao `2`. |

## Diagnostico rapido de cooperado ausente

Para explicar por que uma task nao apareceu, verifique nesta ordem:

1. A task esta em uma das listas do sync principal (`901322296001`, `901321549851`)? Se for task de projeto/lista auxiliar, ela pode alimentar formulario/geracao, mas nao necessariamente o rateio principal.
2. A task tem UC antiga ou UC Aneel?
3. Existe outra task com a mesma UC que ganhou a priorizacao?
4. A distribuidora `84bd83df-2e9f-485f-ae77-0d5c4e02ddf9` resolve para COPEL, AmE, CELESC ou Energisa MS?
5. O Favorecido `0a73e7b8-febe-4982-9263-06efd75612e1` e suportado para essa distribuidora?
6. O status esta na lista de exclusao do rateio?
7. O plano e `Sem faturamento`?
8. O status e `Novo Cooperado` e o mes A ainda esta antes da carencia?
9. A `Projecao de Consumo` tem valor para a UC antiga ou UC Aneel no mes G?
10. No delta, a linha ja existia na aba? Se nao existia, so entra no proximo full sync.
11. O mes esta `Fechado` na `Configuracao`?
12. A meta de geracao existe na `Geracao Total` para o favorecido e mes A?

## Arquivos de codigo mais relevantes

| Arquivo | Papel |
| --- | --- |
| `src/poll.py` | Orquestracao de full/delta, filtros, formularios, geracao total, configuracao e calculos I:M. |
| `src/core/field_map.py` | Mapeamento de campos ClickUp para colunas A:I e rota por distribuidora. |
| `src/core/row_builder.py` | Extracao de campos ClickUp, normalizacao de UC, Favorecido, UC Aneel e task slim. |
| `src/clients/clickup_client.py` | Cliente ClickUp paginado com retries. |
| `src/clients/powerrev_client.py` | Cliente PowerRev, faturas, detalhes, saldo e cache. |
| `src/clients/sheets_manager.py` | Cliente Google Sheets via REST, leitura/escrita por chunks. |
