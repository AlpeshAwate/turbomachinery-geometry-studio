# CFturbo LLM dataset

`output/CFturbo_knowledge.sqlite` is a self-contained knowledge base made from
`CFturbo_en.pdf`. It contains the original PDF, full page text, retrieval-sized
text chunks, an FTS5 keyword index, extracted PNG images, page-to-image links,
and source metadata.

## Query from the command line

```powershell
& '.\.venv\bin\python.exe' query_pdf_dataset.py output\CFturbo_knowledge.sqlite search 'impeller blade angle'
& '.\.venv\bin\python.exe' query_pdf_dataset.py output\CFturbo_knowledge.sqlite page 120
& '.\.venv\bin\python.exe' query_pdf_dataset.py output\CFturbo_knowledge.sqlite image 10 image-10.png
```

FTS5 search syntax supports phrases (`"blade angle"`), prefixes (`impell*`),
and Boolean operators (`pump AND efficiency`). Search results include the PDF
page number so an LLM can cite its evidence.

## Use from application code

Open the database with any SQLite client. Query `chunk_search` for retrieval,
join it to `chunks`, and place the best chunks in the LLM prompt. `pages`
contains complete page text. `images.image_data` contains PNG bytes, while
`page_images` records which page uses each image.

The query helper can also be imported directly by your RAG code:

```python
from query_pdf_dataset import search_database

context = search_database(
    "output/CFturbo_knowledge.sqlite",
    'impeller AND "blade angle"',
    limit=6,
)
```

Rebuild the dataset with:

```powershell
& '.\.venv\bin\python.exe' build_pdf_dataset.py CFturbo_en.pdf output\CFturbo_knowledge.sqlite
```

Validate its structure, embedded source, search index, and PNG payloads with:

```powershell
& '.\.venv\bin\python.exe' verify_pdf_dataset.py output\CFturbo_knowledge.sqlite
```
