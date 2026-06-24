with open("ledger_master.xml", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

name = "United Processors"
idx = content.find(f'NAME="{name}"')
if idx == -1:
    print(f'Could not find NAME="{name}" anywhere in the file.')
else:
    print(content[idx-50:idx+1500])
