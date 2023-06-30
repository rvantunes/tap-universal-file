"""Stream type classes for tap-universal-file."""

from __future__ import annotations

import csv
import json
import re
from typing import Any, Generator

import avro
import avro.datafile
import avro.io
import avro.schema

from tap_unniversal_file.client import FileStream


class DelimitedStream(FileStream):
    """Stream for reading CSVs and TSVs."""

    def get_rows(self) -> Generator[dict[str | Any, str | Any], None, None]:
        """Retrive all rows from all *SVs.

        Yields:
            A dictionary containing information about a row in a *SV.
        """
        for reader_dict in self._get_reader_dicts():
            reader = reader_dict["reader"]
            line_number = 1
            for row in reader:
                yield self.add_additional_info(
                    row=row,
                    file_name=reader_dict["file_name"],
                    line_number=line_number,
                    last_modified=reader_dict["last_modified"],
                )
                line_number += 1

    def get_properties(self) -> dict:
        """Get a list of properties for a *SV file, to be used in creating a schema.

        Each column in the *SV will have its own entry in the schema. All entries will
        be of the form: `'FIELD_NAME': {'type': ['null', 'string']}`

        Returns:
            A list of properties representing a *SV file.
        """
        properties = {}

        for reader_dict in self._get_reader_dicts():
            reader = reader_dict["reader"]
            if reader.fieldnames is None:
                msg = (
                    "Column names could not be read because they don't exist. Try "
                    "manually specifying them using 'delimited_override_headers'."
                )
                raise RuntimeError(msg)
            for field in reader.fieldnames:
                properties.update({field: {"type": ["null", "string"]}})

        return properties

    def _get_reader_dicts(
        self,
    ) -> Generator[dict[str, str | ModifiedDictReader], None, None]:
        quote_character: str = self.config["delimited_quote_character"]
        override_headers: list | None = self.config.get(
            "delimited_override_headers",
            None,
        )

        for file in self.fs_manager.get_files(
            self.starting_replication_key_value
            if self.starting_replication_key_value
            else None,
        ):
            file_name = file["name"]
            if self.config["delimited_delimiter"] == "detect":
                if re.match(".*\\.csv.*", file_name):
                    delimiter = ","
                elif re.match(".*\\.tsv.*", file_name):
                    delimiter = "\t"
                else:
                    msg = (
                        "Configuration option 'delimited_delimiter' is set to 'detect' "
                        "but a non-csv non-tsv file is present. Please manually "
                        "specify 'delimited_delimiter'."
                    )
                    raise RuntimeError(msg)
            else:
                delimiter = self.config["delimited_delimiter"]

            yield {
                "reader": self.ModifiedDictReader(
                    f=self._skip_rows(file_name),
                    delimiter=delimiter,
                    quotechar=quote_character,
                    fieldnames=override_headers,
                    config=self.config,
                ),
                "file_name": file_name,
                "last_modified": file["last_modified"],
            }

    def _skip_rows(self, file: str) -> list[str]:
        with self.fs_manager.filesystem.open(
            path=file,
            mode="rt",
            compression=self.get_compression(file=file),
        ) as f:
            file_list = []
            file_list.extend(f)
        for _ in range(self.config["delimited_header_skip"]):
            if len(file_list) == 0:
                return file_list
            file_list.pop(0)
        for _ in range(self.config["delimited_footer_skip"]):
            if len(file_list) == 0:
                return file_list
            file_list.pop()
        return file_list

    class ModifiedDictReader(csv.DictReader):
        """A modified version of DictReader that detects improperly formatted rows."""

        def __init__(  # noqa: PLR0913
            self,
            f: Any,  # noqa: ANN401
            fieldnames: Any | None = None,
            restkey: Any | None = None,
            restval: Any | None = None,
            dialect: str = "excel",
            config: dict = None,
            *args: Any,
            **kwds: Any,
        ) -> None:
            """Identical to the superclass's method except for defining self.config."""
            super().__init__(f, fieldnames, restkey, restval, dialect, *args, **kwds)
            self.config = config if config is not None else {}

        def __next__(self) -> dict:
            """Identical to the superclass's method except for raising FileFormatErrors.

            Raises:
                FileFormatError: If a row in the *SV has too few entries.
                FileFormatError: If a row in the *SV has too many entries.

            Returns:
                A dictionary containing the records for a row.
            """
            if self.line_num == 0:
                self.fieldnames  # Used for its side-effect. # noqa: B018
            row = next(self.reader)
            self.line_num = self.reader.line_num
            while row == []:
                row = next(self.reader)
            d = dict(zip(self.fieldnames, row))
            lf = len(self.fieldnames)
            lr = len(row)
            if lf < lr:
                d[self.restkey] = row[lf:]
                if self.config["delimited_error_handling"] == "fail":
                    msg = (
                        f"Too few entries at line {self.line_num}. To suppress this "
                        "error, change 'delimited_error_handling' to 'ignore'."
                    )
                    raise RuntimeError(msg)
            elif lf > lr:
                for key in self.fieldnames[lr:]:
                    d[key] = self.restval
                if self.config["delimited_error_handling"] == "fail":
                    msg = (
                        f"Too many entries at line {self.line_num}. To suppress this "
                        "error, change 'delimited_error_handling' to 'ignore'."
                    )
                    raise RuntimeError(msg)
            return d


class JSONLStream(FileStream):
    """Stream for reading JSON files."""

    def get_rows(self) -> Generator[dict[str, Any], None, None]:
        """Retrive all rows from all JSONL files.

        Yields:
            A dictionary containing information about a row in a JSONL file.
        """
        for file in self.fs_manager.get_files(
            self.starting_replication_key_value
            if self.starting_replication_key_value
            else None,
        ):
            file_name = file["name"]
            with self.fs_manager.filesystem.open(
                path=file_name,
                mode="rt",
                compression=self.get_compression(file=file_name),
            ) as f:
                line_number = 1
                for row in f:
                    try:
                        json_row = json.loads(row)
                    except json.JSONDecodeError as e:
                        if self.config["jsonl_error_handling"] == "fail":
                            msg = (
                                f"Invalid format on line {line_number}. "
                                f'JSONDecodeError was "{e}". To suppress this error, '
                                "change 'jsonl_error_handling' to 'ignore'."
                            )
                            raise RuntimeError(msg) from e
                        continue
                    yield self.add_additional_info(
                        row=self._pre_process(json_row),
                        file_name=file_name,
                        line_number=line_number,
                        last_modified=file["last_modified"],
                    )
                    line_number += 1

    def get_properties(self) -> dict:
        """Get a list of properties for a JSONL file, to be used in creating a schema.

        The format of the schema will depend on the jsonl_type_coercion_strategy config
        option, but will always be a dictionary of field names and associated types.

        Returns:
            A list of properties representing a JSONL file.
        """
        properties = {}
        for field in self._get_fields():
            properties.update(self._get_property(field=field))
        return properties

    def _get_property(self, field: str) -> dict[str, dict[str, list[str]]]:
        strategy = self.config["jsonl_type_coercion_strategy"]
        if strategy == "any":
            return {
                field: {
                    "type": [
                        "null",
                        "boolean",
                        "integer",
                        "number",
                        "string",
                        "array",
                        "object",
                    ],
                },
            }
        if strategy == "string":
            return {field: {"type": ["null", "string"]}}
        if strategy == "envelope":
            return {field: {"type": ["null", "object"]}}
        msg = f"The coercion strategy '{strategy}' is not valid."
        raise ValueError(msg)

    def _get_fields(self) -> Generator[str, None, None]:
        strategy = self.config["jsonl_sampling_strategy"]
        if strategy == "first":
            try:
                yield from next(self.get_rows())
            except StopIteration:
                return
            return
        if strategy == "all":
            msg = f"The sampling strategy '{strategy}' has not been implemented."
            raise NotImplementedError(msg)
        msg = f"The sampling strategy '{strategy}' is not valid."
        raise ValueError(msg)

    def _pre_process(self, row: dict[str, Any]) -> dict[str, Any]:
        strategy = self.config["jsonl_type_coercion_strategy"]
        if strategy == "any":
            return row
        if strategy == "string":
            for entry in row:
                row[entry] = str(row[entry])
            return row
        if strategy == "envelope":
            return {"record": row}
        msg = f"The coercion strategy '{strategy}' is not valid."
        raise ValueError(msg)


class AvroStream(FileStream):
    """Stream for reading Avro files."""

    def get_rows(self) -> Generator[dict[str, Any], None, None]:
        """Retrive all rows from all Avro files.

        Yields:
            A dictionary containing information about a row in a Avro file.
        """
        for reader_dict in self._get_reader_dicts():
            reader = reader_dict["reader"]
            line_number = 1
            for row in reader:
                yield self.add_additional_info(
                    row=self._pre_process(row),
                    file_name=reader_dict["file_name"],
                    line_number=line_number,
                    last_modified=reader_dict["last_modified"],
                )
                line_number += 1

    def get_properties(self) -> dict:
        """Get a list of properties for an Avro file, to be used in creating a schema.

        Returns:
            A list of properties representing an Avro file.
        """
        properties = {}
        for field in self._get_fields():
            properties.update(self._get_property(field))
        return properties

    def _get_fields(self) -> Generator[dict | str, None, None]:
        strategy = self.config["avro_type_coercion_strategy"]
        if strategy == "convert":
            for reader_dict in self._get_reader_dicts():
                reader = reader_dict["reader"]
                for field in json.loads(reader.schema)["fields"]:
                    yield field
            return
        if strategy == "envelope":
            yield "record"
            return
        msg = f"The coercion strategy '{strategy}' is not valid."
        raise ValueError(msg)

    def _get_property(self, field: dict | str) -> dict[str, dict[str, list[str]]]:
        strategy = self.config["avro_type_coercion_strategy"]
        if strategy == "convert":
            return {field["name"]: {"type": [self._type_convert(field["type"])]}}
        if strategy == "envelope":
            return {field: {"type": ["null", "object"]}}
        msg = f"The coercion strategy '{strategy}' is not valid."
        raise ValueError(msg)

    def _type_convert(self, field_type: str) -> str:
        if type(field_type) != str:
            msg = f"The field type '{field_type}' has not been implemented."
            raise NotImplementedError(msg)
        if field_type in {"null", "boolean", "string"}:
            return field_type
        if field_type in {"int", "long"}:
            return "integer"
        if field_type in {"float", "double"}:
            return "number"
        if field_type == "bytes":
            return "string"
        msg = f"The field type '{field_type} has not been implemented."
        raise NotImplementedError(msg)

    def _pre_process(self, row: dict[str, Any]) -> dict[str, Any]:
        strategy = self.config["avro_type_coercion_strategy"]
        if strategy == "convert":
            return row
        if strategy == "envelope":
            return {"record": row}
        msg = f"The coercion strategy '{strategy}' is not valid."
        raise ValueError(msg)

    def _get_reader_dicts(
        self,
    ) -> Generator[dict[str, str | avro.datafile.DataFileReader], None, None]:
        for file in self.fs_manager.get_files(
            self.starting_replication_key_value
            if self.starting_replication_key_value
            else None,
        ):
            file_name = file["name"]
            with self.fs_manager.filesystem.open(
                path=file_name,
                mode="rb",
                compression=self.get_compression(file=file_name),
            ) as f:
                yield {
                    "reader": avro.datafile.DataFileReader(f, avro.io.DatumReader()),
                    "file_name": file_name,
                    "last_modified": file["last_modified"],
                }
