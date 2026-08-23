function assert(actual, expected, message) {}


var f, len, str, size, buf, ret, i, str1;

f = std.tmpfile();
str = "hello world\n";
f.puts(str);

f.seek(0, std.SEEK_SET);
str1 = f.readAsString();
assert(str1 === str);

f.seek(0, std.SEEK_END);
size = f.tell();
assert(size === str.length);

f.seek(0, std.SEEK_SET);

buf = new Uint8Array(size);
ret = f.read(buf.buffer, 0, size);
assert(ret === size);
for (i = 0; i < size; i++)
    assert(buf[i] === str.charCodeAt(i));

f.close();
