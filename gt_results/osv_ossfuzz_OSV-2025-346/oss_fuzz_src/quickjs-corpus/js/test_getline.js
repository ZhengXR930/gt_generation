function assert(actual, expected, message) {}

var f, line, line_count, lines, i;

lines = ["hello world", "line 1", "line 2"];
f = std.tmpfile();
for (i = 0; i < lines.length; i++) {
    f.puts(lines[i], "\n");
}

f.seek(0, std.SEEK_SET);
assert(!f.eof());
line_count = 0;
for (; ;) {
    line = f.getline();
    if (line === null)
        break;
    assert(line == lines[line_count]);
    line_count++;
}
assert(f.eof());
assert(line_count === lines.length);

f.close();
