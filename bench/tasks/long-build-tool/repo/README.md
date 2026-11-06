# minibuild

A small make-like tool, because make isn't on the build boxes. Nothing exists yet. Run with `python -m minibuild [options] [targets]`.

Buildfile (read from ./Buildfile, or `-f FILE`):

    # comment
    CC = gcc
    FLAGS = -O2 $(EXTRA)

    .phony: all clean

    all: app

    app: main.o util.o
        $(CC) -o $@ $^

    main.o: main.c util.h
        $(CC) $(FLAGS) -c $< -o $@

    clean:
        rm -f app *.o

- Commands are indented lines (spaces or tabs), run in order with `sh -c` from the Buildfile's folder, each echoed to stdout first. A leading `@` hides the echo. A leading `-` ignores that command's failure.
- `$@` the target, `$<` the first dependency, `$^` all dependencies, `$(VAR)` a variable. `$$` is a literal `$`. Variables can reference others and are expanded when used; an undefined variable is empty. `NAME=value` on the command line overrides the file, and the file overrides nothing else.
- With no targets given, the first target in the file is built.
- A target is rebuilt when its file is missing, any dependency is newer (strictly), or a dependency was rebuilt in this run. A phony target always runs. Each target runs at most once per run, however many paths reach it. A dependency that has no rule and no file is an error: `minibuild: no rule to make 'x' (needed by 'y')`, exit 2.
- A cycle is an error before anything runs: `minibuild: cycle: a -> b -> a`, exit 2.
- A failing command stops the build, exit 1, and prints `minibuild: target 'x' failed (exit N)` to stderr. With `-k`, it keeps going with whatever doesn't depend on the failed target, and still exits 1.
- `-n` prints the commands that would run (with `@` ones too) and runs nothing.
- `-j N` runs up to N targets at once, never starting a target before its dependencies are done.
- When nothing needed doing and targets exist: `minibuild: 'x' is up to date`, exit 0.
- A syntax problem gives `minibuild: FILE:LINE: message`, exit 2, before anything runs: a command with no target before it, or a line that's none of the above.
