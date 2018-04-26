#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Format Identification for Digital Objects."""

from __future__ import print_function

from argparse import ArgumentParser
import hashlib
import sys
from xml.dom import minidom
from xml.etree import ElementTree as ET
import zipfile

from six.moves import cStringIO
from six.moves.urllib.request import urlopen
from six.moves.urllib.parse import urlparse

from .pronomutils import get_local_pronom_versions
from .utils import escape


class NS(object):
    """Helper class for XML name spaces in ElementTree.

    Use like MYNS=NS("{http://some/uri}") and then MYNS(tag1/tag2).
    """

    def __init__(self, uri):
        """Instantiate class with `uri` argument."""
        self.uri = uri

    def __getattr__(self, tag):
        """Append URI to the class attributes."""
        return self.uri + tag

    def __call__(self, path):
        """Define behavior when the instance is used as a function."""
        return "/".join(getattr(self, tag) for tag in path.split("/"))


XHTML = NS("{http://www.w3.org/1999/xhtml}")  # XHTML namespace
TNA = NS("{http://pronom.nationalarchives.gov.uk}")  # TNA namespace


def get_text_tna(element, tag, default=''):
    """Return the text for a tag or path using the TNA namespace."""
    part = element.find(TNA(tag))
    if part is None or part.text is None:
        return default
    return part.text.strip()


def prettify(elem):
    """Return a pretty-printed XML string for the Element."""
    rough_string = ET.tostring(elem, 'UTF-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")


class FormatInfo(object):
    """Convert PRONOM formats into FIDO signatures."""

    def __init__(self, pronom_files):
        """Instantiate class given a list of PRONOM files."""
        self.formats = []
        self.pronom_files = pronom_files

    def save(self, dst=sys.stdout):
        """Write the FIDO XML format definitions to @param dst."""
        tree = ET.ElementTree(ET.Element('formats', {
            'version': '0.3',
            'xmlns:xsi': "http://www.w3.org/2001/XMLSchema-instance",
            'xsi:noNamespaceSchemaLocation': "fido-formats.xsd",
            'xmlns:dc': "http://purl.org/dc/elements/1.1/",
            'xmlns:dcterms': "http://purl.org/dc/terms/"
        }))
        root = tree.getroot()
        for f in self.formats:
            # MdR: this skipped puids without sig, but we want them ALL
            # because puid might be matched on extension
            # if f.find('signature'):
            root.append(f)
        self.indent(root)
        with open(dst, 'wb') as file_:
            file_.write(ET.tostring(root))

    def indent(self, elem, level=0):
        """Indent output."""
        i = "\n" + level * "  "
        if len(elem):
            if not elem.text or not elem.text.strip():
                elem.text = i + "  "
            if not elem.tail or not elem.tail.strip():
                elem.tail = i
            for elem_ in elem:
                self.indent(elem_, level + 1)
            if not elem.tail or not elem.tail.strip():
                elem.tail = i
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = i

    def load_pronom_xml(self, puid_filter=None):
        """Load the pronom XML and convert to fido XML.

        Loads the pronom XML from self.pronom_files and converts it to fido XML.
        As a side-effect, set self.formats to a list of ElementTree.Element.
        If a @param puid is specified, only that one will be loaded.
        """
        formats = []
        try:
            zip_ = zipfile.ZipFile(self.pronom_files, 'r')
            for item in zip_.infolist():
                try:
                    stream = zip_.open(item)
                    # Work is done here!
                    format_ = parse_pronom_xml(stream, puid_filter)
                    if format_ is not None:
                        formats.append(format_)
                finally:
                    stream.close()
        finally:
            try:
                zip_.close()
            except Exception as e:
                print("An error occured loading '{0}' (exception: {1})".format(
                    self.pronom_files, e), file=sys.stderr)
                sys.exit()
        # Replace the formatID with puids in has_priority_over
        if puid_filter is None:
            id_map = {}
            for element in formats:
                puid = element.find('puid').text
                # print "working on puid:",puid
                pronom_id = element.find('pronom_id').text
                id_map[pronom_id] = puid
            for element in formats:
                for rel in element.findall('has_priority_over'):
                    rel.text = id_map[rel.text]

        self.formats = _sort_formats(formats)


# FIXME: I don't think that this quite works yet!
def _sort_formats(formatlist):
    """Sort ``formatlist`` by priority.

    Sort the items in ``formatlist`` based on their priority relationships so
    that higher priority formats appear earlier in the list.
    """
    def compare_formats(f1, f2):
        f1ID = f1.find('puid').text
        f2ID = f2.find('puid').text
        for worse in f1.findall('has_priority_over'):
            if worse.text == f2ID:
                return - 1
        for worse in f2.findall('has_priority_over'):
            if worse.text == f1ID:
                return 1
        if f1ID < f2ID:
            return - 1
        if f1ID == f2ID:
            return 0
        return 1
    return sorted(formatlist, cmp=compare_formats)


def parse_pronom_xml(source, puid_filter=None):
    """Parse PRONOM XML and convert into FIDO XML.

    If a @param puid is specified, only that one will be loaded.
    @return ET.ElementTree Element representing it.
    """
    pronom_xml = ET.parse(source)
    pronom_root = pronom_xml.getroot()
    pronom_format = pronom_root.find(TNA('report_format_detail/FileFormat'))
    fido_format = ET.Element('format')
    # Get the base Format information
    for id_ in pronom_format.findall(TNA('FileFormatIdentifier')):
        type_ = get_text_tna(id_, 'IdentifierType')
        if type_ == 'PUID':
            puid = get_text_tna(id_, 'Identifier')
            ET.SubElement(fido_format, 'puid').text = puid
            if puid_filter and puid != puid_filter:
                return None
    # A bit clumsy.  I want to have puid first, then mime, then container.
    for id_ in pronom_format.findall(TNA('FileFormatIdentifier')):
        type_ = get_text_tna(id_, 'IdentifierType')
        if type_ == 'MIME':
            ET.SubElement(fido_format, 'mime').text = get_text_tna(
                id_, 'Identifier')
        elif type_ == 'PUID':
            puid = get_text_tna(id_, 'Identifier')
            if puid == 'x-fmt/263':
                ET.SubElement(fido_format, 'container').text = 'zip'
            elif puid == 'x-fmt/265':
                ET.SubElement(fido_format, 'container').text = 'tar'
    ET.SubElement(fido_format, 'name').text = get_text_tna(
        pronom_format, 'FormatName')
    ET.SubElement(fido_format, 'version').text = get_text_tna(
        pronom_format, 'FormatVersion')
    ET.SubElement(fido_format, 'alias').text = get_text_tna(
        pronom_format, 'FormatAliases')
    ET.SubElement(fido_format, 'pronom_id').text = get_text_tna(
        pronom_format, 'FormatID')
    # Get the extensions from the ExternalSignature
    for x in pronom_format.findall(TNA('ExternalSignature')):
        ET.SubElement(fido_format, 'extension').text = get_text_tna(
            x, 'Signature')
    for id_ in pronom_format.findall(TNA('FileFormatIdentifier')):
        type_ = get_text_tna(id_, 'IdentifierType')
        if type_ == 'Apple Uniform Type Identifier':
            ET.SubElement(fido_format, 'apple_uid').text = get_text_tna(
                id_, 'Identifier')
    # Handle the relationships
    for x in pronom_format.findall(TNA('RelatedFormat')):
        rel = get_text_tna(x, 'RelationshipType')
        if rel == 'Has priority over':
            ET.SubElement(fido_format, 'has_priority_over').text = (
                get_text_tna(x, 'RelatedFormatID'))
    # Get the InternalSignature information
    for pronom_sig in pronom_format.findall(TNA('InternalSignature')):
        fido_sig = ET.SubElement(fido_format, 'signature')
        ET.SubElement(fido_sig, 'name').text = get_text_tna(
            pronom_sig, 'SignatureName')
        # There are some funny chars in the notes, which caused me trouble
        # and it is a unicode string,
        ET.SubElement(fido_sig, 'note').text = get_text_tna(
            pronom_sig, 'SignatureNote')
        for pronom_pat in pronom_sig.findall(TNA('ByteSequence')):
            fido_pat = ET.SubElement(fido_sig, 'pattern')
            pos = fido_position(get_text_tna(pronom_pat, 'PositionType'))
            bytes_ = get_text_tna(pronom_pat, 'ByteSequenceValue')
            offset = get_text_tna(pronom_pat, 'Offset')
            max_offset = get_text_tna(pronom_pat, 'MaxOffset')
            if not max_offset:
                pass
            # print "working on puid:", puid, ", position: ", pos, "with
            # offset, maxoffset: ", offset, ",", max_offset
            regex = convert_to_regex(bytes_, 'Little', pos, offset,
                                     max_offset)
            # print "done puid", puid
            if regex == "__INCOMPATIBLE_SIG__":
                print("Error: incompatible PRONOM signature found for puid"
                      " {} skipping...".format(puid), file=sys.stderr)
                # remove the empty 'signature' nodes now that the signature
                # is not compatible and thus "regex" is empty
                remove = fido_format.findall('signature')
                for r in remove:
                    fido_format.remove(r)
                continue
            ET.SubElement(fido_pat, 'position').text = pos
            ET.SubElement(fido_pat, 'pronom_pattern').text = bytes_
            ET.SubElement(fido_pat, 'regex').text = regex
    # Get the format details
    fido_details = ET.SubElement(fido_format, 'details')
    ET.SubElement(fido_details, 'dc:description').text = get_text_tna(
        pronom_format, 'FormatDescription')
    ET.SubElement(fido_details, 'dcterms:available').text = get_text_tna(
        pronom_format, 'ReleaseDate')
    ET.SubElement(fido_details, 'dc:creator').text = get_text_tna(
        pronom_format, 'Developers/DeveloperCompoundName')
    ET.SubElement(fido_details, 'dcterms:publisher').text = get_text_tna(
        pronom_format, 'Developers/OrganisationName')
    for x in pronom_format.findall(TNA('RelatedFormat')):
        rel = get_text_tna(x, 'RelationshipType')
        if rel == 'Is supertype of':
            ET.SubElement(fido_details, 'is_supertype_of').text = (
                get_text_tna(x, 'RelatedFormatID'))
    for x in pronom_format.findall(TNA('RelatedFormat')):
        rel = get_text_tna(x, 'RelationshipType')
        if rel == 'Is subtype of':
            ET.SubElement(fido_details, 'is_subtype_of').text = (
                get_text_tna(x, 'RelatedFormatID'))
    ET.SubElement(fido_details, 'content_type').text = (
        get_text_tna(pronom_format, 'FormatTypes'))
    # References
    for x in pronom_format.findall(TNA("Document")):
        r = ET.SubElement(fido_details, 'reference')
        ET.SubElement(r, 'dc:title').text = get_text_tna(x, 'TitleText')
        ET.SubElement(r, 'dc:creator').text = get_text_tna(
            x, 'Author/AuthorCompoundName')
        ET.SubElement(r, 'dc:publisher').text = get_text_tna(
            x, 'Publisher/PublisherCompoundName')
        ET.SubElement(r, 'dcterms:available').text = get_text_tna(
            x, 'PublicationDate')
        for id_ in x.findall(TNA('DocumentIdentifier')):
            type_ = get_text_tna(id_, 'IdentifierType')
            if type_ == 'URL':
                ET.SubElement(r, 'dc:identifier').text = (
                    "http://" + get_text_tna(id_, 'Identifier'))
            else:
                ET.SubElement(r, 'dc:identifier').text = get_text_tna(
                    id_, 'IdentifierType') + ":" + get_text_tna(id_, 'Identifier')
        ET.SubElement(r, 'dc:description').text = get_text_tna(
            x, 'DocumentNote')
        ET.SubElement(r, 'dc:type').text = get_text_tna(x, 'DocumentType')
        ET.SubElement(r, 'dcterms:license').text = '{} {}'.format(
            get_text_tna(x, 'AvailabilityDescription'),
            get_text_tna(x, 'AvailabilityNote'))
        ET.SubElement(r, 'dc:rights').text = get_text_tna(x, 'DocumentIPR')
    # Examples
    for x in pronom_format.findall(TNA("ReferenceFile")):
        rf = ET.SubElement(fido_details, 'example_file')
        ET.SubElement(rf, 'dc:title').text = get_text_tna(
            x, 'ReferenceFileName')
        ET.SubElement(rf, 'dc:description').text = get_text_tna(
            x, 'ReferenceFileDescription')
        checksum = ""
        for id_ in x.findall(TNA('ReferenceFileIdentifier')):
            type_ = get_text_tna(id_, 'IdentifierType')
            if type_ == 'URL':
                # Starting with PRONOM 89, some URLs contain http://
                # and others do not.
                url = get_text_tna(id_, 'Identifier')
                if not urlparse(url).scheme:
                    url = "http://" + url
                ET.SubElement(rf, 'dc:identifier').text = url
                # And calculate the checksum of this resource:
                m = hashlib.md5()
                sock = urlopen(url)
                m.update(sock.read())
                sock.close()
                checksum = m.hexdigest()
            else:
                ET.SubElement(rf, 'dc:identifier').text = '{}:{}'.format(
                    get_text_tna(id_, 'IdentifierType'),
                    get_text_tna(id_, 'Identifier'))
        ET.SubElement(rf, 'dcterms:license').text = ""
        ET.SubElement(rf, 'dc:rights').text = get_text_tna(
            x, 'ReferenceFileIPR')
        checksumElement = ET.SubElement(rf, 'checksum')
        checksumElement.text = checksum
        checksumElement.attrib['type'] = "md5"
    # Record Metadata
    md = ET.SubElement(fido_details, 'record_metadata')
    ET.SubElement(md, 'status').text = 'unknown'
    ET.SubElement(md, 'dc:creator').text = get_text_tna(
        pronom_format, 'ProvenanceName')
    ET.SubElement(md, 'dcterms:created').text = get_text_tna(
        pronom_format, 'ProvenanceSourceDate')
    ET.SubElement(md, 'dcterms:modified').text = get_text_tna(
        pronom_format, 'LastUpdatedDate')
    ET.SubElement(md, 'dc:description').text = get_text_tna(
        pronom_format, 'ProvenanceDescription')
    return fido_format


def fido_position(pronom_position):
    """Return BOF/EOF/VAR instead of the more verbose pronom position names."""
    if pronom_position == 'Absolute from BOF':
        return 'BOF'
    if pronom_position == 'Absolute from EOF':
        return 'EOF'
    if pronom_position == 'Variable':
        return 'VAR'
    if pronom_position == 'Indirect From BOF':
        return 'IFB'
    # to make sure FIDO does not crash (IFB aftermath)
    sys.stderr.write("Unknown pronom PositionType:" + pronom_position)
    return 'VAR'


def _convert_err_msg(msg, c, i, chars, buf):
    return ("Conversion: {0}: char='{1}', at pos {2} in \n  {3}\n  "
            "{4}^\nBuffer = {5}".format(
                msg, c, i, chars, i * ' ', buf.getvalue()))


def do_byte(chars, i, littleendian, buf, esc=True):
    """Convert two chars[i] and chars[i+1] into a byte.

    @return a tuple (byte, 2)
    """
    c1 = '0123456789ABCDEF'.find(chars[i].upper())
    c2 = '0123456789ABCDEF'.find(chars[i + 1].upper())
    if (c1 < 0 or c2 < 0):
        raise Exception(
            _convert_err_msg('bad byte sequence', chars[i:i + 2], i, chars,
                             buf))
    if littleendian:
        val = chr(16 * c1 + c2)
    else:
        val = chr(c1 + 16 * c2)
    if esc:
        return (escape(val), 2)
    return (val, 2)


# Python now allows regex repetitions to be max out somewhere between 4 and 4.3
# billion, cf. https://bugs.python.org/issue13169#msg180499
MAX_REGEX_REPS = 4e9


def calculate_repetition(char, pos, offset, maxoffset):
    """Recursively calculates offset/maxoffset repetition.

    This function only has an effect when one or both offsets is greater than
    MAX_REGEX_REPS bytes (4GB). See: https://bugs.python.org/issue13169.
    """
    calcbuf = cStringIO()

    calcremain = False
    offsetremain = 0
    maxoffsetremain = 0

    if offset is not None and int(offset) > MAX_REGEX_REPS:
        offsetremain = str(int(offset) - MAX_REGEX_REPS)
        offset = str(int(MAX_REGEX_REPS))
        calcremain = True
    if maxoffset is not None and int(maxoffset) > MAX_REGEX_REPS:
        maxoffsetremain = str(int(maxoffset) - MAX_REGEX_REPS)
        maxoffset = str(int(MAX_REGEX_REPS))
        calcremain = True

    if pos == "BOF" or pos == "EOF":
        if offset != '0':
            calcbuf.write(char + '{' + str(offset))
            if maxoffset is not None:
                calcbuf.write(',' + maxoffset)
            calcbuf.write('}')
        elif maxoffset is not None:
            calcbuf.write(char + '{0,' + maxoffset + '}')

    if pos == "IFB":
        if offset != '0':
            calcbuf.write(char + '{' + str(offset))
            if maxoffset is not None:
                calcbuf.write(',' + maxoffset)
            calcbuf.write('}')
            if maxoffset is not None:
                calcbuf.write(',}')
        elif maxoffset is not None:
            calcbuf.write(char + '{0,' + maxoffset + '}')

    if calcremain:  # recursion happens here
        calcbuf.write(
            calculate_repetition(char, pos, offsetremain, maxoffsetremain))

    val = calcbuf.getvalue()
    calcbuf.close()
    return val


def do_all_bitmasks(chars, i, littleendian, buf):
    """(byte & bitmask) == bitmask."""
    return do_any_all_bitmasks(
        chars, i, lambda byt, bitmask: ((byt & bitmask) == bitmask),
        littleendian, buf)


def do_any_bitmasks(chars, i, littleendian, buf):
    """(byte & bitmask) != 0."""
    return do_any_all_bitmasks(
        chars, i, lambda byt, bitmask: ((byt & bitmask) != 0),
        littleendian, buf)


def do_any_all_bitmasks(chars, i, predicate, littleendian, buf):
    """Convert an all/any bitmask string (e.g., &07) to a Python regex.

    Whether the bitmask is "all" (&) or "any" (~) is determined by the supplied
    function ``predicate``. As an example, '&07' means 'match bytes with all
    first three bits set' or 'match all bytes where (byte & 0x07) == 0x07' or,
    in Python, match this disjunctive regex::

        >>> ('0x7|0xf|0x17|0x1f|0x27|0x2f|0x37|0x3f|0x47|0x4f|0x57|0x5f|0x67|'
        ...  '0x6f|0x77|0x7f|0x87|0x8f|0x97|0x9f|0xa7|0xaf|0xb7|0xbf|0xc7|0xcf|'
        ...  '0xd7|0xdf|0xe7|0xef|0xf7|0xff')

    See https://github.com/nishihatapalmer/byteseek/wiki/Regular-Expression-Syntax#all-bitmasks
    and https://github.com/nishihatapalmer/byteseek/wiki/Regular-Expression-Syntax#any-bitmasks
    """
    byt, inc = do_byte(chars, i + 1, littleendian, buf, esc=False)
    bitmask = ord(byt)
    regex = '({})'.format(
        '|'.join(['\\x' + hex(byte)[2:].zfill(2) for byte in range(0x100)
                  if predicate(byte, bitmask)]))
    return regex, inc + 1


def convert_to_regex(chars, endianness='', pos='BOF', offset='0', maxoffset=''):
    """Convert to regular expression.

    Endianness is not used.

    @param chars, a pronom bytesequence, into a
    @return regular expression.
    """
    if 'Big' in endianness:
        littleendian = False
    else:
        littleendian = True
    if len(offset) == 0:
        offset = '0'
    if len(maxoffset) == 0:
        maxoffset = None
    if maxoffset == '0':
        maxoffset = None
    buf = cStringIO()
    # If a regex starts with (?s), it is equivalent to DOTALL.
    buf.write("(?s)")
    i = 0
    state = 'start'
    if 'BOF' in pos:
        buf.write('\\A')  # start of regex
        buf.write(calculate_repetition('.', pos, offset, maxoffset))

    if 'IFB' in pos:
        buf.write('\\A')
        buf.write(calculate_repetition('.', pos, offset, maxoffset))

    while True:
        if i == len(chars):
            break
        # print _convert_err_msg(state, chars[i], i, chars, buf)
        if state == 'start':
            if chars[i].isalnum():
                state = 'bytes'
            elif chars[i] == '&':
                state = 'all-bitmask'
            elif chars[i] == '~':
                state = 'any-bitmask'
            elif chars[i] == '[' and chars[i + 1] == '!':
                state = 'non-match'
            elif chars[i] == '[':
                state = 'bracket'
            elif chars[i] == '{':
                state = 'curly'
            elif chars[i] == '(':
                state = 'paren'
            elif chars[i] in '*+?':
                state = 'specials'
            else:
                raise Exception(
                    _convert_err_msg('Illegal character in start', chars[i], i,
                                     chars, buf))
        elif state == 'bytes':
            (byt, inc) = do_byte(chars, i, littleendian, buf)
            buf.write(byt)
            i += inc
            state = 'start'
        elif state == 'all-bitmask':
            (byt, inc) = do_all_bitmasks(chars, i, littleendian, buf)
            buf.write(byt)
            i += inc
            state = 'start'
        elif state == 'any-bitmask':
            (byt, inc) = do_any_bitmasks(chars, i, littleendian, buf)
            buf.write(byt)
            i += inc
            state = 'start'
        elif state == 'non-match':
            buf.write('(?!')
            i += 2
            while True:
                if chars[i].isalnum():
                    (byt, inc) = do_byte(chars, i, littleendian, buf)
                    buf.write(byt)
                    i += inc
                elif chars[i] == '&':
                    (byt, inc) = do_all_bitmasks(chars, i, littleendian, buf)
                    buf.write(byt)
                    i += inc
                elif chars[i] == '~':
                    (byt, inc) = do_any_bitmasks(chars, i, littleendian, buf)
                    buf.write(byt)
                    i += inc
                elif chars[i] == ']':
                    break
                else:
                    raise Exception(
                        _convert_err_msg('Illegal character in non-match',
                                         chars[i], i, chars, buf))
            buf.write(')')
            i += 1
            state = 'start'

        elif state == 'bracket':
            try:
                buf.write('[')
                i += 1
                (byt, inc) = do_byte(chars, i, littleendian, buf)
                buf.write(byt)
                i += inc
                # assert(chars[i] == ':')
                if chars[i] != ':':
                    return "__INCOMPATIBLE_SIG__"
                buf.write('-')
                i += 1
                (byt, inc) = do_byte(chars, i, littleendian, buf)
                buf.write(byt)
                i += inc
                # assert(chars[i] == ']')
                if chars[i] != ']':
                    return "__INCOMPATIBLE_SIG__"
                buf.write(']')
                i += 1
            except Exception:
                print(_convert_err_msg('Illegal character in bracket',
                                       chars[i], i, chars, buf))
                raise
            if i < len(chars) and chars[i] == '{':
                state = 'curly-after-bracket'
            else:
                state = 'start'
        elif state == 'paren':
            buf.write('(?:')
            i += 1
            while True:
                if chars[i].isalnum():
                    (byt, inc) = do_byte(chars, i, littleendian, buf)
                    buf.write(byt)
                    i += inc
                elif chars[i] == '|':
                    buf.write('|')
                    i += 1
                elif chars[i] == ')':
                    break
                # START fix FIDO-20
                elif chars[i] == '[':
                    buf.write('[')
                    i += 1
                    (byt, inc) = do_byte(chars, i, littleendian, buf)
                    buf.write(byt)
                    i += inc
                    # assert(chars[i] == ':')
                    if chars[i] != ':':
                        return "__INCOMPATIBLE_SIG__"
                    buf.write('-')
                    i += 1
                    (byt, inc) = do_byte(chars, i, littleendian, buf)
                    buf.write(byt)
                    i += inc

                    # assert(chars[i] == ']')
                    if chars[i] != ']':
                        return "__INCOMPATIBLE_SIG__"
                    buf.write(']')
                    i += 1
                else:
                    raise Exception(
                        _convert_err_msg(
                            'Current state = \'{0}\' : Illegal character in'
                            ' paren'.format(state), chars[i], i, chars, buf))
            buf.write(')')
            i += 1
            state = 'start'
            # END fix FIDO-20
        elif state in ['curly', 'curly-after-bracket']:
            # {nnnn} or {nnn-nnn} or {nnn-*}
            # {nnn} or {nnn,nnn} or {nnn,}
            # when there is a curly-after-bracket, then the {m,n} applies to
            # the bracketed item
            # The above, while sensible, appears to be incorrect.  A '.' is
            # always needed for droid equiv behavior
            # if state == 'curly':
            buf.write('.')
            buf.write('{')
            i += 1                # skip the (
            while True:
                if chars[i].isalnum():
                    buf.write(chars[i])
                    i += 1
                elif chars[i] == '-':
                    buf.write(',')
                    i += 1
                elif chars[i] == '*':  # skip the *
                    i += 1
                elif chars[i] == '}':
                    break
                else:
                    raise Exception(
                        _convert_err_msg('Illegal character in curly',
                                         chars[i], i, chars, buf))
            buf.write('}')
            i += 1                # skip the )
            state = 'start'
        elif state == 'specials':
            if chars[i] == '*':
                buf.write('.*')
                i += 1
            elif chars[i] == '+':
                buf.write('.+')
                i += 1
            elif chars[i] == '?':
                if chars[i + 1] != '?':
                    raise Exception(
                        _convert_err_msg('Illegal character after ?',
                                         chars[i + 1], i + 1, chars, buf))
                buf.write('.?')
                i += 2
            state = 'start'
        else:
            raise Exception('Illegal state {0}'.format(state))

    if 'EOF' in pos:
        buf.write(calculate_repetition('.', pos, offset, maxoffset))
        buf.write('\\Z')

    val = buf.getvalue()
    buf.close()
    return val


def run(input_=None, output=None, puid=None):
    """Convert PRONOM formats into FIDO signatures."""
    versions = get_local_pronom_versions()

    if input_ is None:
        input_ = versions.get_zip_file()
    if output is None:
        output = versions.get_signature_file()

    info = FormatInfo(input_)
    info.load_pronom_xml(puid)
    info.save(output)
    print('Converted {0} PRONOM formats to FIDO signatures'.format(
        len(info.formats)), file=sys.stderr)


def main(args=None):
    """Main CLI entrypoint."""
    if args is None:
        args = sys.argv[1:]

    parser = ArgumentParser(
        description='Produce the FIDO format XML that is loaded at run-time')
    parser.add_argument('-input', default=None,
                        help='Input file, a Zip containing PRONOM XML files')
    parser.add_argument('-output', default=None, help='Ouptut file')
    parser.add_argument('-puid', default=None,
                        help='A particular PUID record to extract')
    args = parser.parse_args(args)

    run(input_=args.input, output=args.output, puid=args.puid)


if __name__ == '__main__':
    main()
