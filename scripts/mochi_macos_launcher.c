/* Run the checkout's Python in-process so Cocoa uses this app bundle's identity. */
#include <Python.h>
#include <stdio.h>
#include <unistd.h>

#ifndef MOCHI_MAIN_MODULE
#define MOCHI_MAIN_MODULE "deskpet"
#endif

int main(int argc, char **argv) {
    if (chdir(MOCHI_CHECKOUT) != 0) {
        perror("Could not open the Mochi checkout");
        return 1;
    }

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;
    /* Let Python use the checkout as sys.path[0], as with python -m. */
    argv[0] = "-m";
    PyStatus status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (!PyStatus_Exception(status)) {
        status = PyConfig_SetBytesString(&config, &config.program_name, MOCHI_PYTHON);
    }
    if (!PyStatus_Exception(status)) {
        /* The Observation and recovery children use the normal Python CLI. */
        status = PyConfig_SetBytesString(&config, &config.executable, MOCHI_PYTHON);
    }
    if (!PyStatus_Exception(status)) {
        status = PyConfig_SetBytesString(&config, &config.run_module, MOCHI_MAIN_MODULE);
    }
    if (!PyStatus_Exception(status)) {
        status = Py_InitializeFromConfig(&config);
    }
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        Py_ExitStatusException(status);
    }
    return Py_RunMain();
}
