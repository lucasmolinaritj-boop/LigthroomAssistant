# Relatório do Schema — Catálogo Analisado

Catálogo anexado: `testa esse bosta.lrcat` (Lightroom Classic, catalog schema com 115 tabelas).
Fotos no catálogo: **3** (`5D3_3324.jpg`, `5D3_3323.jpg`, `5D3_3329.jpg`).

## Onde ficam Exposure e Shadows

As configurações de revelação (Develop Settings) de cada foto ficam na tabela
`Adobe_imageDevelopSettings`, coluna `text`, como uma tabela Lua legível, por exemplo:

```
s = { ...
Exposure2012 = 1.18,
...
Shadows2012 = 24,
... }
```

- O catálogo usa o Process Version atual (2012+), então os campos ativos são
  **`Exposure2012`** e **`Shadows2012`** dentro dessa string (não os campos legados
  `Exposure`/`Shadows`, que ficam zerados quando a foto já foi migrada).
- A relação com a foto correta é feita pela coluna `image` de
  `Adobe_imageDevelopSettings`, que aponta para `Adobe_images.id_local`.

## Caminho do arquivo de cada foto

```
Adobe_images.rootFile -> AgLibraryFile.id_local
AgLibraryFile.folder -> AgLibraryFolder.id_local
AgLibraryFolder.rootFolder -> AgLibraryRootFolder.id_local
caminho completo = AgLibraryRootFolder.absolutePath + AgLibraryFolder.pathFromRoot + AgLibraryFile.idx_filename
```

No catálogo anexado, as fotos apontam para `M:/Meu Drive/Homepicz/Fotos do dia/22725/internas/`
(um HD/drive específico da máquina de origem — os arquivos de imagem em si não vêm dentro do
catálogo, por isso não estavam presentes neste ambiente de testes). O programa está preparado
para ler esses arquivos normalmente quando executado na máquina onde as fotos realmente existem.

## EXIF

Tabela `AgHarvestedExifMetadata` (relacionada por `image` -> `Adobe_images.id_local`):
aperture, focalLength, isoSpeedRating, shutterSpeed, GPS, etc.

## Presets

O catálogo **não guarda um nome de preset explícito** por foto em uma coluna dedicada.
O nome do preset aplicado foi encontrado na tabela `Adobe_libraryImageDevelopHistoryStep`
(histórico de revelação), coluna `name`, que registra entradas como:

```
"Importar/MASTERBLASTER2025-2 (10/07/2026 15:17:39)"
```

O programa extrai o nome do preset a partir dessa string (removendo a parte de
data/hora entre parênteses e o prefixo do tipo de ação). Neste catálogo, as 3
fotos foram associadas ao preset **`MASTERBLASTER2025-2`** — que corresponde a um
dos arquivos `.xmp` encontrados na pasta de presets anexada
(`MASTERBLASTER2025-2 (ESSE É O BASE E DEVE SER APLICADO EM TODAS AS FOTOS).xmp`).

Quando uma foto não tem entrada no histórico com nome, o programa registra no log
que o preset não foi encontrado e continua funcionando normalmente (usa apenas
EXIF/características visuais para a sugestão).

## Estrutura de pastas do projeto (segundo RAR anexado)

- `Pasta de Catalogos para alimentar sistema (...)` → pasta de alimentação do banco
- `Pasta de catalogos a editar` → catálogos novos a processar
- (pasta de catálogos editados não existia ainda — o programa a cria automaticamente)
- `presets padrão (...)` → arquivos `.xmp` de referência
- `banco de dados (...)` → onde o programa guarda seu SQLite próprio

O programa detecta essas pastas automaticamente por palavras-chave no nome
("alimentar", "editar", "editado", "preset", "banco", "log", "backup"), então
funciona mesmo que os nomes exatos variem.
