var s, i, len = 100;
s = new Set();
for (i = 0; i < len; i++) {
    s.add(String(i), i);
}
for (i = 0; i < len; i++) {
    if (!s.has(String(i)))
        throw Error("bug in Set");
}
