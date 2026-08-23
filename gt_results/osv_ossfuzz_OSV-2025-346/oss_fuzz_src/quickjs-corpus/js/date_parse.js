var x0 = 0, dx = 0;
var x1 = x0 - x0 % 1000;
var x2 = -x0;
var x3 = -x1;
var d0 = new Date(x0);
var d1 = new Date(x1);
var d2 = new Date(x2);
var d3 = new Date(x3);
if (Date.parse(d0.toISOString()) != x0 || Date.parse(d1.toGMTString()) != x1 || Date.parse(d1.toString()) != x1 || Date.parse(d2.toISOString()) != x2 || Date.parse(d3.toGMTString()) != x3 || Date.parse(d3.toString()) != x3) {
    console.log("Date.parse error for " + x0);
} else {
    dx = (dx * 1.1 + 1) >> 0;
    x0 = (x0 + dx) % 8.64e15;
}
