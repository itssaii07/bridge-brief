"""multipart/form-data parsing on the standard library.

The web app accepts photographs from a browser form. ``cgi.FieldStorage`` is
deprecated and gone in Python 3.13, so this parses the body with the ``email``
package, which has handled MIME multipart for decades and ships everywhere this
project runs.

It is deliberately strict: a size ceiling is enforced before the body is read,
file parts are returned as bytes and never written anywhere by this module, and
a malformed body is an error rather than a partial result.
"""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser

#: The most a single inspection upload may carry, all photographs together.
MAX_BODY_BYTES = 200 * 1024 * 1024


class MultipartError(ValueError):
    """The request body is not usable multipart/form-data."""


@dataclass(frozen=True)
class UploadedFile:
    field: str
    filename: str
    content_type: str
    data: bytes


def parse(content_type: str | None, body: bytes) -> tuple[dict[str, str], list[UploadedFile]]:
    """Split a multipart body into text fields and files.

    Returns:
        ``(fields, files)``. A text field that appears more than once keeps its
        last value; files are returned in the order they were sent.

    Raises:
        MultipartError: if the content type is not multipart/form-data, the
            boundary is missing, or the body does not parse.
    """
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        raise MultipartError("expected a multipart/form-data upload")
    if "boundary=" not in content_type:
        raise MultipartError("the upload has no multipart boundary")
    if len(body) > MAX_BODY_BYTES:
        raise MultipartError(
            f"the upload is {len(body) / 2**20:.0f} MB; the limit is "
            f"{MAX_BODY_BYTES / 2**20:.0f} MB")

    header = f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode("latin-1")
    try:
        message = BytesParser(policy=policy.HTTP).parsebytes(header + body)
    except Exception as exc:  # the email parser raises a range of types
        raise MultipartError(f"the upload body did not parse: {exc}") from exc
    if not message.is_multipart():
        raise MultipartError("the upload body is not multipart")

    fields: dict[str, str] = {}
    files: list[UploadedFile] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            if not payload:
                continue          # an empty file input, not a file
            files.append(UploadedFile(
                field=str(name), filename=str(filename),
                content_type=part.get_content_type(), data=payload))
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[str(name)] = payload.decode(charset, errors="replace")
    return fields, files
