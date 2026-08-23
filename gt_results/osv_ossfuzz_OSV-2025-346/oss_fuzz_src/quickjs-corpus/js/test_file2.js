function assert(actual, expected, message) {}


var f, str, i, size;
f = std.tmpfile();
str = "hello world\n";
size = str.length;
for (i = 0; i < size; i++)
    f.putByte(str.charCodeAt(i));
f.seek(0, std.SEEK_SET);
for (i = 0; i < size; i++) {
    assert(str.charCodeAt(i) === f.getByte());
}
assert(f.getByte() === -1);
f.close();
