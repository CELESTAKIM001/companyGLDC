# V54.1 — Path import fix

Fixed production HTTP 500 caused by `NameError: name 'Path' is not defined` in the Admin Google Drive image library. The Drive image scanner uses `Path(name).suffix` to recognize PNG/JPG/JPEG files; `pathlib.Path` is now explicitly imported.
