var th, i;

/* just test that a timer can be inserted and removed */
th = [];
for (i = 0; i < 3; i++)
    th[i] = os.setTimeout(function () {
    }, 1000);
for (i = 0; i < 3; i++)
    os.clearTimeout(th[i]);
