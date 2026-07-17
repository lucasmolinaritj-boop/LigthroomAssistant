# Lightroom Assistant — Exposure & Shadows

Protótipo funcional para Windows que aprende com fotos já editadas no Lightroom
Classic e sugere/aplica automaticamente **Exposure** e **Shadows** em catálogos
novos, com base nas fotos mais parecidas já editadas antes.

## Instalação (Windows)

1. Instale o [Python 3.10+](https://www.python.org/downloads/) marcando a opção
   "Add Python to PATH".
2. Extraia este ZIP em qualquer pasta.
3. Dê duplo clique em **`INSTALAR.bat`** (cria um ambiente virtual e instala
   PySide6, Pillow e numpy).
4. Dê duplo clique em **`INICIAR.bat`** para abrir o programa.

## Como usar

1. Clique em **"Selecionar pasta"** e escolha a pasta raiz do seu projeto (a
   pasta que contém as subpastas de catálogos, presets, banco, etc). O
   programa detecta automaticamente pastas com nomes parecidos com:
   - "...alimentar..." → catálogos já editados, usados para alimentar o banco
   - "...editar..." → catálogos novos, ainda não editados
   - "...editado..." → onde os catálogos prontos serão salvos
   - "...preset..." → seus presets `.xmp`
   - "...banco..." → onde o banco de dados próprio do programa fica
   - "...backup..." / "...log..." → pastas de apoio
2. Clique em **"Alimentar banco"** para ler todos os catálogos `.lrcat` da
   pasta de alimentação e extrair Exposure, Shadows, preset aplicado, EXIF e
   características visuais de cada foto para o banco SQLite local.
3. Clique em **"Editar catálogo"** para processar os catálogos `.lrcat` novos:
   para cada foto, o programa busca as fotos mais parecidas no banco, calcula
   Exposure/Shadows sugeridos, aplica **somente esses dois valores** em uma
   **cópia** do catálogo (o original nunca é tocado), confere a integridade do
   arquivo e move o resultado para a pasta de catálogos editados.
4. Acompanhe o progresso na barra de progresso e o que está acontecendo na
   área de log (também salva em `logs/`).

## Como funciona a sugestão (resumo técnico)

Para cada foto, são calculadas features baratas e rápidas: brilho médio,
contraste, histograma e percentis de luminância (10/25/50/75/90), além de
EXIF (ISO, abertura, distância focal, velocidade do obturador) e o preset
aplicado (identificado pelo histórico de revelação do catálogo).

Ao editar uma foto nova, o programa:

1. Calcula a distância entre a foto nova e todas as fotos do banco
   (normalizando cada característica), priorizando fotos com o **mesmo
   preset** — mas sem nunca excluir fotos de outros presets caso não haja
   suficientes.
2. Pega as 5–10 fotos mais parecidas e calcula uma **média ponderada** de
   Exposure e Shadows (peso inversamente proporcional à distância).
3. Aplica uma pequena correção baseada na diferença de brilho/luminância
   entre a foto nova e as referências usadas.
4. Se a foto nova ou o banco não tiverem características visuais disponíveis
   (por exemplo, arquivo de imagem não encontrado no disco), o programa cai
   para uma média mais simples por preset — e registra isso no log — em vez
   de falhar.

## Segurança

- O catálogo original **nunca** é editado diretamente — o programa sempre
  trabalha em cima de uma cópia.
- Depois de escrever os novos valores, é executado `PRAGMA integrity_check`
  na cópia; se a verificação falhar, o processo é interrompido e nada é
  movido para a pasta de editados.
- Os valores gravados são lidos de volta e conferidos antes de considerar o
  catálogo pronto.
- Todos os outros ajustes de revelação da foto são preservados — apenas
  `Exposure2012` e `Shadows2012` são alterados.

## Estrutura deste pacote

```
app.py                  - ponto de entrada (roda a interface gráfica)
gui.py                  - interface PySide6 (4 abas originais)
gui_tools.py            - v12: aba "Ferramentas e Saúde" (8 sub-abas, ver abaixo)
core.py                 - toda a lógica (leitura do catálogo, banco, sugestão, escrita)
safety.py               - v12: quarentena de outliers, aliases de lente, cache seguro,
                          saúde do banco, estatísticas, backup/restauração, logs
                          estruturados, benchmark — tudo aditivo, nunca chamado pelo
                          caminho normal de sugestão/aprendizado a menos que ativado
requirements.txt        - dependências Python
INSTALAR.bat            - cria o ambiente e instala dependências
INICIAR.bat             - abre o programa
banco/                  - banco de dados SQLite próprio do programa
banco/backups_v12/      - backups versionados do banco (v12)
logs/                   - logs de execução (v12 adiciona logs rotativos por categoria)
backups/                - área de trabalho temporária para cópias de catálogo
tests/test_v12_safety.py - suíte de regressão v11-vs-v12 (pytest)
relatorio_schema.md     - o que foi descoberto no schema do catálogo anexado
relatorio_teste.md      - resultado dos testes executados com o catálogo real
DELIVERY_v12.md         - relatório de entrega da v12 (o que mudou, como usar, como reverter)
```

## v12 — Segurança, Diagnóstico e Performance

Uma nova aba **"🛠 Ferramentas e Saúde"** foi adicionada, com 8 sub-abas:

- **Saúde do Banco** — visão geral do estado do banco (versões de schema, contagens,
  integridade, últimos backup/benchmark).
- **Estatísticas** — estatísticas detalhadas (mediana, média, desvio, percentis) por
  parâmetro, com agrupamento opcional por lente/preset/câmera/ISO, exportável em CSV/JSON.
- **Outliers** — fotos com valores estatisticamente incomuns ficam em quarentena em vez
  de serem aprendidas silenciosamente; nada é apagado, você aprova/ignora/restaura.
- **Lentes e Aliases** — mapeamento manual de nomes de lente alternativos para o nome
  canônico usado nos presets, como um complemento (nunca substituto) da detecção automática.
- **Cache** — acelera leituras repetidas; qualquer falha do cache cai de volta ao cálculo
  normal, então nunca é a única fonte de dados.
- **Backups** — backup manual ou automático do banco, com verificação de integridade e
  restauração segura (o estado atual é salvo antes de qualquer restauração).
- **Benchmark** — execução manual, somente leitura; nunca treina nem grava correções.
- **Logs** — pasta de logs rotativos por categoria e exportação de um pacote de
  diagnóstico (sem o banco de dados).

**Garantia importante:** todo o algoritmo de sugestão, o casamento por KNN, o
aprendizado de viés (bias), a correspondência de lentes e a escrita no catálogo
continuam **exatamente iguais** aos da v11 — os recursos acima são aditivos e, por
padrão, desativados no nível do motor (`core.py`); a interface os liga apenas
quando você realmente os usa. Veja `DELIVERY_v12.md` para detalhes técnicos.

## Limitações conhecidas deste protótipo

- A análise visual (histograma/brilho/contraste) depende de conseguir abrir o
  arquivo de imagem no caminho gravado no catálogo. Se a foto estiver em um
  HD externo desconectado, ou o caminho tiver mudado, o programa usa um
  fallback por preset/EXIF em vez de falhar, e registra isso no log.
- O nome do preset é obtido a partir do histórico de revelação do catálogo
  (não existe uma coluna dedicada de "preset" no schema do Lightroom). Se o
  histórico não tiver nome registrado, o preset fica marcado como não
  identificado, mas o programa continua funcionando normalmente.
- Este é um MVP: a similaridade usa distância euclidiana normalizada sobre um
  conjunto pequeno e rápido de características (sem CLIP/rede neural), como
  pedido no escopo do protótipo.
