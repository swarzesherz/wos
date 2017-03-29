from __future__ import unicode_literals

import codecs
import logging
import sys

PY2 = sys.version_info[0] == 2
if PY2:
    from io import open
    from unicodecsv import DictReader
else:
    basestring = str
    from csv import DictReader

from .tags import has_item_per_line

logger = logging.getLogger(__name__)

__all__ = [
    "get_reader",
    "read",
    "PlainTextReader",
    "ReadError",
    "TabDelimitedReader",
]


class ReadError(Exception):
    pass


def sniff_file(fh, length=10, offset=0):
    sniff = fh.read(length)
    fh.seek(offset)

    return sniff


def sniff_encoding(fh):
    """Guess encoding of file `fh`

    Note that this function is optimized for WoS text files and may yield
    incorrect results for other text files.

    :param fh: File opened in binary mode
    :type fh: file object
    :return: best guess encoding as str

    """
    sniff = sniff_file(fh)

    # WoS files typically include a BOM, which we want to strip from the actual
    # data. The encodings 'utf-8-sig' and 'utf-16' do this for UTF-8 and UTF-16
    # respectively. When dealing with files with BOM, avoid the encodings
    # 'utf-8' (which is fine for non-BOM UTF-8), 'utf-16-le', and 'utf-16-be'.
    # See e.g. http://stackoverflow.com/a/8827604
    encodings = {codecs.BOM_UTF16: 'utf-16',
                 codecs.BOM_UTF8: 'utf-8-sig'}
    for bom, encoding in encodings.items():
        if sniff.startswith(bom):
            return encoding
    # WoS export files are either UTF-8 or UTF-16
    return 'utf-8'


def get_reader(fh):
    """Get appropriate reader for the file type of `fh`"""
    sniff = sniff_file(fh)

    if sniff.startswith("FN "):
        reader = PlainTextReader
    elif "\t" in sniff:
        reader = TabDelimitedReader
    else:
        # XXX TODO Raised for empty file -- not very elegant
        raise ReadError("Could not determine appropriate reader for file "
                        "{}".format(fh))
    return reader


def read(fname, using=None, encoding=None, **kwargs):
    """Read WoS export file ('tab-delimited' or 'plain text')

    :param fname: name(s) of the WoS export file(s)
    :type fname: str or iterable of strings
    :param using:
        class used for reading `fname`. If None, we try to automatically
        find best reader
    :param str encoding:
        encoding of the file. If None, we try to automatically determine the
        file's encoding
    :return:
        iterator over records in `fname`, where each record is a field code -
        value dict

    """
    if not isinstance(fname, basestring):
        # fname is an iterable of file names
        for actual_fname in fname:
            for record in read(actual_fname):
                yield record

    else:
        if encoding is None:
            with open(fname, 'rb') as fh:
                encoding = sniff_encoding(fh)

        if using is None:
            with open(fname, 'rt', encoding=encoding) as fh:
                reader_class = get_reader(fh)
        else:
            reader_class = using

        with open(fname, 'rt', encoding=encoding) as fh:
            reader = reader_class(fh, **kwargs)
            for record in reader:
                yield record


class TabDelimitedReader(object):

    def __init__(self, fh, **kwargs):
        """Create a reader for tab-delimited file `fh` exported fom WoS

        If you do not know the encoding of a file, the :func:`.read` function
        tries to automatically Do The Right Thing.

        :param fh: WoS tab-delimited file, opened in text mode(!)
        :type fh: file object

        """
        # unicodecsv expects byte strings but TabDelimitedReader works with
        # Unicode strings. Hence, we encode everything.
        if PY2:
            def as_utf8(f):
                return (line.encode('utf-8') for line in f)
            kwargs.update({'encoding': 'utf-8'})
            fh = as_utf8(fh)
        # Delimiter should be byte string in Py2 -- wrapping it with str(), we
        # get bytes in Py2 and Unicode string in Py3.
        self.reader = DictReader(fh, delimiter=str("\t"), **kwargs)

    def next(self):
        record = next(self.reader)
        # Since WoS files have a spurious tab at the end of each line, we
        # may get a 'ghost' None key.
        try:
            del record[None]
        except KeyError:
            pass
        return record
    __next__ = next

    def __iter__(self):
        return self


class PlainTextReader(object):

    def __init__(self, fh, subdelimiter="; "):
        """Create a reader for WoS plain text file `fh`

        If you do not know the format of a file, the :func:`.read` function
        tries to automatically Do The Right Thing.

        :param fh: WoS plain text file, opened in text mode(!)
        :type fh: file object
        :param str subdelimiter:
            string delimiting different parts of a multi-part field,
            like author(s)

        """
        self.fh = fh
        self.subdelimiter = subdelimiter
        self.version = "1.0"  # Expected version of WoS plain text format
        self.current_line = 0

        line = self._next_nonempty_line()
        if not line.startswith("FN"):
            raise ReadError("Unknown file format")

        line = self._next_nonempty_line()
        label, version = line.split()
        if label != "VR" or version != self.version:
            raise ReadError("Unknown version: expected {} "
                            "but got {}".format(self.version, version))

    def _next_line(self):
        """Get next line as Unicode"""
        self.current_line += 1
        return next(self.fh).rstrip("\n")

    def _next_nonempty_line(self):
        """Get next line that is not empty"""
        line = ""
        while not line:
            line = self._next_line()
        return line

    def _next_record_lines(self):
        """Gather lines that belong to one record"""
        lines = []
        while True:
            try:
                line = self._next_nonempty_line()
            except StopIteration:
                raise ReadError("Encountered EOF before 'EF' marker")
            if line.startswith("EF"):
                if lines:  # We're in the middle of a record!
                    raise ReadError(
                        "Encountered unexpected end of file marker EF on "
                        "line {}".format(self.current_line))
                else:  # End of file
                    raise StopIteration
            if line.startswith("ER"):  # end of record
                return lines
            else:
                lines.append(line)

    def _format_values(self, heading, values):
        if has_item_per_line[heading]:  # Iterable field with one item per line
            return self.subdelimiter.join(values)
        else:
            return " ".join(values)

    def next(self):
        record = {}
        values = []
        heading = ""
        lines = self._next_record_lines()

        # Parse record, this is mostly handling multi-line fields
        for line in lines:
            if not line.startswith("  "):  # new field
                # Add previous field, if available, to record
                if heading:
                    record[heading] = self._format_values(heading, values)
                heading, v = line.split(None, 1)
                values = [v]
            else:
                values.append(line.strip())

        # Add last field
        record[heading] = self._format_values(heading, values)

        return record
    __next__ = next

    def __iter__(self):
        return self
