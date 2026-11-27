import time, docx, utils, book, loc
orig = utils.http_get
def timed(url, *a, **k):
    t = time.time()
    try:
        r = orig(url, *a, **k)
        print(f"{time.time()-t:6.1f}s {r.status_code} {url[:90]}")
        return r
    except Exception as e:
        print(f"{time.time()-t:6.1f}s LỖI {type(e).__name__} {url[:90]}")
        raise
utils.http_get = book.http_get = timed
d = docx.Document()
p = d.add_paragraph("Held, D., McGrew, A., Goldblatt, D., & Perraton, J. (1999). Global transformations: Politics, economics and culture. Stanford University Press.")
t = time.time()
print("KẾT QUẢ", book.process_book_paragraph(d, p, p.text), f"{time.time()-t:.1f}s")
for c in d.comments: print("COMMENT:", c.text)