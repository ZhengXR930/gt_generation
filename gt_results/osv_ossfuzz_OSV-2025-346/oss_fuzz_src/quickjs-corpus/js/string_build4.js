/* incremental string construction with multiple reference */
var i, r, s;
r = "";
for (i = 0; i < 1000; i++) {
    s = r;
    r += "x";
}
