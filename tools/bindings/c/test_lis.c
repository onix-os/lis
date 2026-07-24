#include "lis.h"
#include <stdio.h>
#include <assert.h>
#include <string.h>

int main(void) {
    lis_document_t doc = {0};
    doc.version = LIS_SPEC_VERSION;
    doc.hostname = "tron";
    doc.arch = LIS_ARCH_X86_64;
    doc.firmware = LIS_FIRMWARE_UEFI;

    assert(strcmp(doc.version, "0.1.0") == 0);
    assert(strcmp(doc.hostname, "tron") == 0);
    assert(doc.arch == LIS_ARCH_X86_64);
    assert(doc.firmware == LIS_FIRMWARE_UEFI);

    printf("C header binding test passed! LIS version: %s\n", doc.version);
    return 0;
}
