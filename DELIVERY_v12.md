# Lightroom Assistant — v12: Segurança, Diagnóstico & Performance

## Resumo

v12 adiciona 8 subsistemas aditivos de segurança/diagnóstico/performance e uma
nova aba de UI, sem alterar o comportamento do algoritmo de sugestão, do KNN,
do aprendizado de viés (bias), da correspondência de lentes ou da escrita no
catálogo. Todos os testes de regressão v11-vs-v12 passam (18/18).

## Arquivos alterados

- `core.py` — mudanças mínimas e aditivas:
  - Import opcional de `safety` (mesmo padrão já usado para `numpy`/`Pillow`;
    se `safety.py` falhar ao importar, o app funciona exatamente como v11).
  - `init_database()` chama `safety.init_safety_schema()` após a migração v11
    (dentro de try/except — falha aqui nunca impede a abertura do banco).
  - `feed_database_from_catalog(..., outlier_check: bool = False)` — novo
    parâmetro opcional, **desligado por padrão**. Quando ligado, cada foto é
    avaliada por `safety.evaluate_photo_for_outliers` antes da inserção;
    fotos "suspeitas" ou "inválidas" vão para quarentena em vez de serem
    inseridas em `photos` (nada é descartado — tudo fica disponível para
    revisão/restauração na aba Outliers).
  - `apply_xmp_by_lens(..., alias_lookup=None)` — novo parâmetro opcional.
    Só é consultado quando o casador de lentes já existente (`_best_lens_match`)
    não encontra NADA — nunca compete com ou substitui um match exato/por
    substring/por tokens que a v11 já teria encontrado.
  - Novo helper `insert_photo_snapshot()` — usado exclusivamente pela ação
    explícita "Restaurar ao banco" da aba Outliers.
- `gui.py` — mudanças mínimas:
  - Import opcional de `gui_tools`.
  - Novos defaults em `AppConfig` (`feature_outlier_detection`,
    `feature_lens_aliases`, `feature_cache`, todos `True` — a v12 vem "ligada"
    na UI porque o próprio detector nunca atua com poucos dados/banco vazio).
  - `BancoTab` passa `outlier_check=` na chamada de feed.
  - `PresetLenteTab` passa `alias_lookup=` na chamada de aplicação por lente.
  - `MainWindow` adiciona a 5ª aba **"🛠 Ferramentas e Saúde"**, sem tocar,
    renomear ou reordenar as 4 abas existentes.
- `gui_tools.py` (**novo**) — a aba "Ferramentas e Saúde" com 8 sub-abas
  (Saúde do Banco, Estatísticas, Outliers, Lentes e Aliases, Cache, Backups,
  Benchmark, Logs). Cada sub-aba é isolada: uma falha em uma nunca derruba o
  app nem as outras abas.
- `safety.py` (**novo**) — toda a lógica v12, sem depender de PySide6 (pode
  ser testada com `python3` puro, igual ao `core.py`).
- `tests/test_v12_safety.py` (**novo**) — suíte de regressão pytest.

## Novas tabelas de banco (todas aditivas — nenhuma tabela/coluna v11 foi alterada)

| Tabela | Propósito |
|---|---|
| `safety_schema_version` | Versionamento próprio do schema v12 (começa em 1), separado do `schema_version` v11. |
| `outlier_quarantine` | Fotos com parâmetros estatisticamente incomuns, com snapshot completo para restauração. |
| `lens_aliases` | Mapeamento manual nome-original → nome-canônico de lente. |
| `safe_cache` | Cache genérico chave→valor com hits/namespace, nunca fonte única de dados. |
| `backup_metadata` | Índice dos backups (arquivo real fica em `banco/backups_v12/`). |
| `benchmark_runs` | Histórico de execuções de benchmark (somente informativo). |

## Resultado dos testes de regressão

```
$ cd .local_debug/la5_pkg && python3 -m pytest tests/ -v
======================== 18 passed in ~5.5s ========================
```

Cobertura confirmada:
- Migração de schema é 100% aditiva; abrir um banco v11 existente preserva
  todos os dados e ganha as tabelas v12 sem perda.
- `feed_database_from_catalog` com `outlier_check=False` (explícito) produz
  bit-a-bit o mesmo resultado que a chamada v11 sem o parâmetro.
- O detector de outliers nunca marca nada com poucas amostras (`< 8`), aceita
  variação estilística normal, e só marca valores genuinamente extremos.
- O alias de lente só é consultado quando o casador v11 não encontra nada —
  nunca sobrepõe um match exato já encontrado.
- Cache: leitura/escrita funcionam, e uma entrada corrompida cai de volta ao
  cálculo normal sem quebrar nada.
- Backup → restauração é íntegro; restaurar a partir de um backup corrompido
  é detectado e o banco original permanece intocado (nenhuma reversão parcial).
- Benchmarks (técnico e de estabilidade) não alteram `photos`, `ai_suggestions`
  nem `correction_log` — verificado via checksum antes/depois.
- Uma falha simulada dentro do subsistema de outliers durante um feed real
  não impede a foto de ser processada (isolamento de falhas confirmado).
- Uma falha simulada em `init_safety_schema` não impede a abertura do banco.

Também foi feito um smoke-test manual da interface (`QT_QPA_PLATFORM=offscreen`):
a janela principal abre com as 5 abas esperadas, a 5ª aba monta as 8 sub-abas,
e cada sub-aba executa seu `refresh()` inicial sem lançar exceção.

## Como atualizar (upgrade)

Basta trocar os arquivos `.py` pelo conteúdo desta versão e abrir o programa
normalmente — na primeira execução, `init_database()` aplica o schema v12
automaticamente sobre o banco existente, sem perder nenhum dado. Nenhuma ação
manual é necessária.

## Como reverter (rollback)

- **Reverter só o código:** substitua `core.py`/`gui.py` pelos arquivos da v11
  e remova `safety.py`/`gui_tools.py`. O banco continua funcionando (as
  tabelas v12 extras ficam ali sem uso, mas não atrapalham nada v11 lê).
- **Reverter o banco também:** use a aba **Backups** (v12) para restaurar um
  snapshot anterior, ou copie manualmente `banco/lightroom_assistant.db` de
  um backup salvo em `banco/backups_v12/`.

## Onde as coisas ficam

- Backups: `banco/backups_v12/*.db` (índice em `backup_metadata`).
- Cache: dentro do próprio banco, tabela `safe_cache` (limpável pela aba Cache).
- Logs: `logs/` — o arquivo de log original da v11 continua existindo e
  recebendo as mesmas mensagens de sempre; a v12 adiciona handlers rotativos
  adicionais (`app.log`, `database.log`, `processing.log`, `errors.log`,
  `benchmark.log`) na mesma pasta, sem remover o comportamento anterior.
- Quarentena de outliers: tabela `outlier_quarantine`, visível na aba Outliers.

## Correção pós-entrega: falso positivo no casamento de lentes

Depois da entrega inicial, foi identificado (e corrigido novamente) o bug de
casamento de lentes que já havia sido corrigido antes: duas lentes
genuinamente diferentes da mesma linha (ex.: "Sony FE 24-70mm F2.8 GM" e
"Sony FE 70-200mm F2.8 GM") podiam ser tratadas como a mesma lente pelo
casamento por similaridade de tokens, porque compartilhar marca+encaixe+
abertura (4 de 5 palavras) já era suficiente para passar nos limiares de
similaridade — mesmo com distância focal diferente.

**Correção:** `_score_lens_candidate` agora rejeita imediatamente qualquer
match por similaridade de tokens quando as duas lentes têm uma distância
focal explícita e ela diverge, não importa quantas outras palavras
coincidam. Casos legítimos (ex.: mesma lente com um sufixo "II" de revisão)
continuam funcionando normalmente. Dois novos testes de regressão
(`test_lens_token_match_never_ignores_a_conflicting_focal_length` e
`test_lens_token_match_still_works_when_focal_length_agrees`) cobrem esse
cenário especificamente, para este bug não voltar uma terceira vez.

## Correção pós-entrega 2: aplicação de preset agora é auditada e verificada de ponta a ponta

Investigação adicional do relato "termina o processamento mas os presets
não aparecem aplicados no Lightroom" encontrou uma segunda causa raiz,
além do bug de casamento de lentes acima: `apply_xmp_by_lens` contava uma
foto como "aplicada" só porque a instrução `UPDATE` foi executada sem
lançar exceção — nunca confirmava que a linha certa foi realmente afetada,
nem relia o texto do catálogo depois de gravar para confirmar que os
campos de lente persistiram. Um caso em particular era enganoso: se a
substituição produzisse um bloco Lua estruturalmente inválido, o código já
revertia para o texto original (correto), mas o resultado era então
comparado a "nenhuma alteração necessária" e contado como
`PRESET_JA_IDENTICO` — uma falha real de gravação relatada como se fosse
inofensiva.

**Correções aplicadas em `core.py`:**

* `apply_xmp_to_develop_text` agora tem um caminho interno
  (`_apply_xmp_to_develop_text_impl`) que diferencia "nada precisava
  mudar" de "a validação estrutural falhou e revertemos" — o segundo caso
  nunca mais é contado como idêntico/aplicado.
* Cada foto é gravada dentro de um `SAVEPOINT` próprio: depois do
  `UPDATE`, o código confere `rowcount == 1` (senão: `UPDATE_NAO_AFETOU_LINHA`)
  e então relê o texto do próprio catálogo para confirmar que os campos
  críticos de lente (`LensProfileEnable`, `AutoLateralCA`,
  `LensProfileSetup`, `LensProfileName`) realmente persistiram com o valor
  esperado. Se a verificação falhar, a foto é revertida (`ROLLBACK TO`) e
  marcada como `VERIFICACAO_FALHOU` — nunca contada como aplicada.
* O relatório CSV (`<catalogo>_relatorio_lentes.csv`) agora tem uma linha
  por foto considerada (não só as ignoradas) com todas as colunas
  pedidas: arquivo, image_id, lente_catalogo, lente_configurada,
  tipo_match, preset_xmp, campos_xmp, alterado, verificado, motivo, erro.

Três novos testes de regressão cobrem esse fluxo de ponta a ponta usando
um catálogo SQLite mínimo real (não apenas as funções isoladas). Suíte
completa: 26/26 testes passando.

## Fora do escopo (confirmado com o usuário no plano aprovado)

- Não há mudança na precisão/algoritmo de sugestão em si — o benchmark de
  estabilidade mede apenas determinismo, não "qualidade".
- Não há novo motor de correspondência de lentes — o alias é um complemento,
  não uma reimplementação do `_best_lens_match`.
