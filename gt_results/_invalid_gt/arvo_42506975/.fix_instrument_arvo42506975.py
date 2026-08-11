from pathlib import Path
p = Path('/src/dnsmasq/src/hash-questions.c')
text = p.read_text()
marker2 = '      fprintf(stderr,\n              "ASSERT_EVT point=hash_questions_call name_argument=%p name_capacity=%zu maxdname_limit=%u\\n",\n              (void *)name, strlen(name) + 1, (unsigned)MAXDNAME);\n\n'
if 'ASSERT_EVT point=hash_questions_call' not in text:
    text = text.replace('      char *cp, c;\n\n', '      char *cp, c;\n\n' + marker2, 1)
p.write_text(text)

p = Path('/src/dnsmasq/src/rfc1035.c')
text = p.read_text()
marker3 = '\t  fprintf(stderr, "ASSERT_EVT point=extract_name_root cp_base=%p name_len_after_label=%u\\n",\n\t          (void *)cp, (unsigned)namelen);\n'
if 'ASSERT_EVT point=extract_name_root' not in text:
    text = text.replace('\t  if (!CHECK_LEN(header, p, plen, l))\n\t    return 0;\n\t  \n', '\t  if (!CHECK_LEN(header, p, plen, l))\n\t    return 0;\n' + marker3 + '\t  \n', 1)
if 'ASSERT_EVT point=extract_name_sink' not in text:
    needle = "\t\tif (c != 0 && c != '.')\n\t\t  *cp++ = c;\n"
    repl = "\t\tif (c != 0 && c != '.') {\n\t\t  fprintf(stderr,\n\t\t          \"ASSERT_EVT point=extract_name_sink write_offset_after=%zu name_capacity=%zu\\n\",\n\t\t          (size_t)((cp + 1) - (unsigned char *)name), strlen(name) + 1);\n\t\t  *cp++ = c;\n\t\t}\n"
    idx = text.find(needle)
    if idx != -1:
        text = text[:idx] + repl + text[idx + len(needle):]
p.write_text(text)
