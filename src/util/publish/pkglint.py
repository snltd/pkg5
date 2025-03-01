#!/usr/bin/python3 -Es
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

#
# Copyright (c) 2010, 2024, Oracle and/or its affiliates.
#

try:
    import pkg.site_paths

    pkg.site_paths.init()
    import argparse
    import codecs
    import logging
    import sys
    import gettext
    import locale
    import traceback
    import warnings

    from pkg.client.api_errors import InvalidPackageErrors
    from pkg.misc import PipeError
    from pkg.client.pkgdefs import EXIT_OK, EXIT_OOPS, EXIT_BADOPT, EXIT_FATAL

    import pkg.lint.engine as engine
    import pkg.lint.log as log
    import pkg.fmri as fmri
    import pkg.manifest
    import pkg.misc as misc
    import pkg.client.api_errors as apx
    import pkg.client.transport.exception as tx
except KeyboardInterrupt:
    import sys

    sys.exit(1)  # EXIT_OOPS

logger = None


def error(message=""):
    """Emit an error message prefixed by the command name."""
    misc.emsg("pkglint: {0}".format(message))

    if logger is not None:
        logger.error(_("Error: {0}").format(message))


def msg(message):
    logger.info(message)


def debug(message):
    logger.debug(message)


def main_func():
    """Start pkglint."""

    global logger

    usage = _(
        "\n"
        "        %(prog)s [-b branch] [-c cache_dir] [-f file]\n"
        "            [-l uri ...] [-p regexp] [-r uri ...] [-v]\n"
        "            [-e extension_path ...]\n"
        "            manifest ...\n"
        "        %(prog)s -L"
    )
    parser = argparse.ArgumentParser(usage=usage)

    parser.add_argument(
        "-b",
        dest="release",
        metavar="branch",
        help=_("branch to use from lint and reference repositories"),
    )
    parser.add_argument(
        "-c",
        dest="cache",
        metavar="cache_dir",
        help=_("directory to use as a repository cache"),
    )
    parser.add_argument(
        "-f",
        dest="config",
        metavar="file",
        help=_("specify an alternative pkglintrc file"),
    )
    parser.add_argument(
        "-l",
        dest="lint_uris",
        metavar="uri",
        default=[],
        action="append",
        help=_("lint repository URI"),
    )
    parser.add_argument(
        "-L",
        dest="list_checks",
        action="store_true",
        help=_("list checks configured for this session and exit"),
    )
    parser.add_argument(
        "-p",
        dest="pattern",
        metavar="regexp",
        help=_("pattern to match FMRIs in lint URI"),
    )
    parser.add_argument(
        "-r",
        dest="ref_uris",
        metavar="uri",
        default=[],
        action="append",
        help=_("reference repository URI"),
    )
    parser.add_argument(
        "-e",
        dest="extension_path",
        metavar="extension_path",
        action="append",
        help=_("extension_path"),
    )
    parser.add_argument(
        "-v",
        dest="verbose",
        action="store_true",
        help=_("produce verbose output, overriding settings in pkglintrc"),
    )
    parser.add_argument("manifests", nargs="*")

    args = parser.parse_args()

    # without a cache option, we can't access repositories, so expect
    # local manifests.
    if not (args.cache or args.list_checks) and not args.manifests:
        parser.error(
            _("Required -c option missing, no local manifests provided.")
        )

    logger = logging.getLogger("pkglint")
    ch = logging.StreamHandler(sys.stdout)

    if args.verbose:
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)

    else:
        logger.setLevel(logging.INFO)
        ch.setLevel(logging.INFO)

    logger.addHandler(ch)

    lint_logger = log.PlainLogFormatter()
    try:
        if not args.list_checks:
            msg(_("Lint engine setup..."))
        lint_engine = engine.LintEngine(
            lint_logger,
            config_file=args.config,
            verbose=args.verbose,
            extension_path=args.extension_path,
        )

        if args.list_checks:
            list_checks(
                lint_engine.checkers,
                lint_engine.excluded_checkers,
                args.verbose,
            )
            return EXIT_OK

        if (args.lint_uris or args.ref_uris) and not args.cache:
            parser.error(
                _("Required -c option missing when using repositories.")
            )

        manifests = []
        if args.manifests:
            manifests = read_manifests(args.manifests, lint_logger)
            if None in manifests or lint_logger.produced_lint_msgs():
                error(_("Fatal error in manifest - exiting."))
                return EXIT_OOPS
        lint_engine.setup(
            ref_uris=args.ref_uris,
            lint_uris=args.lint_uris,
            lint_manifests=manifests,
            cache=args.cache,
            pattern=args.pattern,
            release=args.release,
        )

        msg(_("Starting lint run..."))

        lint_engine.execute()
        lint_engine.teardown()
        lint_logger.close()

    except engine.LintEngineSetupException as err:
        # errors during setup are likely to be caused by bad
        # input or configuration, not lint errors in manifests.
        error(err)
        return EXIT_BADOPT

    except engine.LintEngineException as err:
        error(err)
        return EXIT_OOPS

    if lint_logger.produced_lint_msgs():
        return EXIT_OOPS
    else:
        return EXIT_OK


def list_checks(checkers, exclude, verbose=False):
    """Prints a human-readable version of configured checks."""

    # used for justifying output
    width = 28

    def get_method_desc(method, verbose):
        if "pkglint_desc" in method.__dict__ and not verbose:
            return method.pkglint_desc
        else:
            return "{0}.{1}.{2}".format(
                method.__self__.__class__.__module__,
                method.__self__.__class__.__name__,
                method.__func__.__name__,
            )

    def emit(name, value):
        msg("{0} {1}".format(name.ljust(width), value))

    def print_list(items):
        k = list(items.keys())
        k.sort()
        for lint_id in k:
            emit(lint_id, items[lint_id])

    include_items = {}
    exclude_items = {}

    for checker in checkers:
        for m, lint_id in checker.included_checks:
            include_items[lint_id] = get_method_desc(m, verbose)

    for checker in exclude:
        for m, lint_id in checker.excluded_checks:
            exclude_items[lint_id] = get_method_desc(m, verbose)
        for m, lint_id in checker.included_checks:
            exclude_items[lint_id] = get_method_desc(m, verbose)

    for checker in checkers:
        for m, lint_id in checker.excluded_checks:
            exclude_items[lint_id] = get_method_desc(m, verbose)

    if include_items or exclude_items:
        if verbose:
            emit(_("NAME"), _("METHOD"))
        else:
            emit(_("NAME"), _("DESCRIPTION"))
        print_list(include_items)

        if exclude_items:
            msg(_("\nExcluded checks:"))
            print_list(exclude_items)


def read_manifests(names, lint_logger):
    """Read a list of filenames, return a list of Manifest objects."""

    manifests = []
    for filename in names:
        data = None
        # borrowed code from publish.py
        lines = []  # giant string of all input lines
        linecnts = []  # tuples of starting line no., ending line no
        linecounter = 0  # running total
        try:
            f = codecs.open(filename, "rb", "utf-8")
            data = f.read()
        except UnicodeDecodeError as e:
            lint_logger.critical(
                _(
                    "Invalid file {file}: "
                    "manifest not encoded in UTF-8: {err}"
                ).format(file=filename, err=e),
                msgid="lint.manifest002",
            )
            continue
        except IOError as e:
            lint_logger.critical(
                _("Unable to read manifest file {file}: {err}").format(
                    file=filename, err=e
                ),
                msgid="lint.manifest001",
            )
            continue
        lines.append(data)
        linecnt = len(data.splitlines())
        linecnts.append((linecounter, linecounter + linecnt))
        linecounter += linecnt

        manifest = pkg.manifest.Manifest()
        try:
            manifest.set_content(content="\n".join(lines))
        except pkg.actions.ActionError as e:
            lineno = e.lineno
            for i, tup in enumerate(linecnts):
                if lineno > tup[0] and lineno <= tup[1]:
                    lineno -= tup[0]
                    break
            else:
                lineno = "???"

            lint_logger.critical(
                _("Error in {file} line: {ln}: {err} ").format(
                    file=filename, ln=lineno, err=str(e)
                ),
                "lint.manifest002",
            )
            manifest = None
        except InvalidPackageErrors as e:
            lint_logger.critical(
                _("Error in file {file}: {err}").format(
                    file=filename, err=str(e)
                ),
                "lint.manifest002",
            )
            manifest = None

        if manifest and "pkg.fmri" in manifest:
            try:
                manifest.fmri = pkg.fmri.PkgFmri(manifest["pkg.fmri"])
            except fmri.IllegalFmri as e:
                lint_logger.critical(
                    _("Error in file {file}: {err}").format(
                        file=filename, err=e
                    ),
                    "lint.manifest002",
                )
            if manifest.fmri:
                if not manifest.fmri.version:
                    lint_logger.critical(
                        _(
                            "Error in file {0}: "
                            "pkg.fmri does not include a "
                            "version string"
                        ).format(filename),
                        "lint.manifest003",
                    )
                else:
                    manifests.append(manifest)

        elif manifest:
            lint_logger.critical(
                _("Manifest {0} does not declare fmri.").format(filename),
                "lint.manifest003",
            )
        else:
            manifests.append(None)
    return manifests


if __name__ == "__main__":
    misc.setlocale(locale.LC_ALL, "", error)
    gettext.install("pkg", "/usr/share/locale")
    misc.set_fd_limits(printer=error)

    # By default, hide all warnings from users.
    if not sys.warnoptions:
        warnings.simplefilter("ignore")

    try:
        __ret = main_func()
    except (PipeError, KeyboardInterrupt):
        # We don't want to display any messages here to prevent
        # possible further broken pipe (EPIPE) errors.
        __ret = EXIT_BADOPT
    except SystemExit:
        raise
    except (apx.InvalidDepotResponseException, tx.TransportFailures) as __e:
        error(__e)
        __ret = EXIT_BADOPT
    except Exception:
        traceback.print_exc()
        error(misc.get_traceback_message())
        __ret = EXIT_FATAL

    sys.exit(__ret)

# Vim hints
# vim:ts=4:sw=4:et:fdm=marker
