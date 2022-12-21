# Development environment

## Setting up

In order to prepare the development environment, please follow the steps below:

1. Install the Python 3.10 interpreter and pip package manager.
    * Optionally create a Python virtual environment with `python3 -m venv venv` in the project directory and activate it using generated script: `. venv/bin/activate`.
2. Install all required libraries with `pip3 install -r requirements-dev.txt`.
3. Optionally, install all precommit hooks with `pre-commit install`. This will automatically run the linter before commits.

## Using scripts

The development environment contains a number of scripts which are run in CI, but are also intended for local use. They are:

### run\_tests

Runs the unit tests. By default, every available test is run. Tests from a specific file can be run using the following call (`test_transactions` is used as an example):

```
scripts/run_tests test_transactions
```

One can even run a specific test class from a file:

```
scripts/run_tests test_transactions.TestScheduler
```

Or a specific test method:

```
scripts/run_tests test_transactions.TestScheduler.test_single
```

The argument to `run_tests` is actually used to search within the full names of tests. The script runs all the tests which match the query. Thanks to this, if a given test class name is unique, just the class name can be used as an argument.

The `run_tests` script has the following options:

* `-l`, `--list` -- lists available tests. This option is helpful, e.g., to find a name of a test generated using the `parameterized` package.
* `-t`, `--trace` -- generates waveforms in the `vcd` format and `gtkw` files for the `gtkwave` tool. The files are saved in the `test/__traces__/` directory. Useful for debugging and test-driven development.
* `-v`, `--verbose` -- makes the test runner more verbose. It will, for example, print the names of all the tests being run.

### lint.sh

Checks the code formatting and typing. It should be run as follows:

```
scripts/lint.sh subcommand [filename...]
```

The following main subcommands are available:

* `format` -- reformats the code using `black`.
* `check_format` -- verifies code formatting using `black` and `flake8`.
* `check_types` -- verifies typing using `pyright`.
* `verify` -- runs all checks. The same set of checks is run in CI.

When confronted with `would reformat [filename]` message from `black` you may run:

```
black --diff [filename]
```
This way you may display the changes `black` would apply to `[filename]` if you chose the `format` option for `lint.sh` script. This may help you locate the formatting issues.