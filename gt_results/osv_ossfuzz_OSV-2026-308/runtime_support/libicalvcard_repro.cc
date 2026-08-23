#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
extern "C" {
#include "libicalvcard/vcardparser.h"
#include "libicalvcard/vcardcomponent.h"
}
int main(int argc, char **argv) {
  if (argc != 2) return 2;
  std::ifstream in(argv[1], std::ios::binary);
  std::string data((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
  char *buf = (char*)malloc(data.size() + 1);
  if (!buf) return 3;
  memcpy(buf, data.data(), data.size());
  buf[data.size()] = '\0';
  vcardcomponent *comp = vcardparser_parse_string(buf);
  if (comp) vcardcomponent_free(comp);
  free(buf);
  return 0;
}
