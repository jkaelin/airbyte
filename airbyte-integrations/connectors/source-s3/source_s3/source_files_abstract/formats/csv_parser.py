#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#

import csv
import json
import os
import tempfile
from typing import Any, BinaryIO, Iterator, Mapping, Optional, TextIO, Tuple, Union

import pyarrow
import pyarrow as pa
import six
from pyarrow import csv as pa_csv

from ...utils import run_in_external_process
from ..file_info import FileInfo
from .abstract_file_parser import AbstractFileParser
from .csv_spec import CsvFormat

MAX_CHUNK_SIZE = 50.0 * 1024 ** 2  # in bytes
TMP_FOLDER = tempfile.mkdtemp()


class CsvParser(AbstractFileParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.format_model = None

    @property
    def is_binary(self):
        return True

    @property
    def format(self) -> CsvFormat:
        if self.format_model is None:
            self.format_model = CsvFormat.parse_obj(self._format)
        return self.format_model

    def _read_options(self):
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.csv.ReadOptions.html
        build ReadOptions object like: pa.csv.ReadOptions(**self._read_options())
        """
        return {
            **{"block_size": self.format.block_size, "encoding": self.format.encoding},
            **json.loads(self.format.advanced_options),
        }

    def _parse_options(self):
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.csv.ParseOptions.html
        build ParseOptions object like: pa.csv.ParseOptions(**self._parse_options())
        """

        return {
            "delimiter": self.format.delimiter,
            "quote_char": self.format.quote_char,
            "double_quote": self.format.double_quote,
            "escape_char": self.format.escape_char,
            "newlines_in_values": self.format.newlines_in_values,
        }

    def _convert_options(self, json_schema: Mapping[str, Any] = None):
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.csv.ConvertOptions.html
        build ConvertOptions object like: pa.csv.ConvertOptions(**self._convert_options())
        :param json_schema: if this is passed in, pyarrow will attempt to enforce this schema on read, defaults to None
        """
        check_utf8 = self.format.encoding.lower().replace("-", "") == "utf8"

        convert_schema = self.json_schema_to_pyarrow_schema(json_schema) if json_schema is not None else None
        return {
            **{"check_utf8": check_utf8, "column_types": convert_schema},
            **json.loads(self.format.additional_reader_options),
        }

    def get_inferred_schema(self, file: Union[TextIO, BinaryIO]) -> Mapping[str, Any]:
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.csv.open_csv.html
        This now uses multiprocessing in order to timeout the schema inference as it can hang.
        Since the hanging code is resistant to signal interrupts, threading/futures doesn't help so needed to multiprocess.
        https://issues.apache.org/jira/browse/ARROW-11853?page=com.atlassian.jira.plugin.system.issuetabpanels%3Aall-tabpanel
        """

        def infer_schema_process(
            file_sample: str, read_opts: dict, parse_opts: dict, convert_opts: dict
        ) -> Tuple[dict, Optional[Exception]]:
            """
            we need to reimport here to be functional on Windows systems since it doesn't have fork()
            https://docs.python.org/3.7/library/multiprocessing.html#contexts-and-start-methods
            This returns a tuple of (schema_dict, None OR Exception).
            If return[1] is not None and holds an exception we then raise this in the main process.
            This lets us propagate up any errors (that aren't timeouts) and raise correctly.
            """
            try:
                import tempfile

                import pyarrow as pa

                # writing our file_sample to a temporary file to then read in and schema infer as before
                with tempfile.TemporaryFile() as fp:
                    fp.write(file_sample)
                    fp.seek(0)
                    streaming_reader = pa.csv.open_csv(
                        fp, pa.csv.ReadOptions(**read_opts), pa.csv.ParseOptions(**parse_opts), pa.csv.ConvertOptions(**convert_opts)
                    )
                    schema_dict = {field.name: field.type for field in streaming_reader.schema}

            except Exception as e:
                # we pass the traceback up otherwise the main process won't know the exact method+line of error
                return (None, e)
            else:
                return (schema_dict, None)

        # boto3 objects can't be pickled (https://github.com/boto/boto3/issues/678)
        # and so we can't multiprocess with the actual fileobject on Windows systems
        # we're reading block_size*2 bytes here, which we can then pass in and infer schema from block_size bytes
        # the *2 is to give us a buffer as pyarrow figures out where lines actually end so it gets schema correct
        schema_dict = self._get_schema_dict(file, infer_schema_process)
        return self.json_schema_to_pyarrow_schema(schema_dict, reverse=True)

    def _get_schema_dict(self, file, infer_schema_process):
        if not self.format.infer_datatypes:
            return self._get_schema_dict_without_inference(file)
        self.logger.debug("inferring schema")
        file_sample = file.read(self._read_options()["block_size"] * 2)
        return run_in_external_process(
            fn=infer_schema_process,
            timeout=4,
            max_timeout=60,
            logger=self.logger,
            args=[
                file_sample,
                self._read_options(),
                self._parse_options(),
                self._convert_options(),
            ],
        )

    # TODO Rename this here and in `_get_schema_dict`
    def _get_schema_dict_without_inference(self, file):
        self.logger.debug("infer_datatypes is False, skipping infer_schema")
        delimiter = self.format.delimiter
        quote_char = self.format.quote_char
        reader = csv.reader([six.ensure_text(file.readline())], delimiter=delimiter, quotechar=quote_char)
        field_names = next(reader)
        return {field_name.strip(): pyarrow.string() for field_name in field_names}

    def stream_records(self, file: Union[TextIO, BinaryIO]) -> Iterator[Mapping[str, Any]]:
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.csv.open_csv.html
        PyArrow returns lists of values for each column so we zip() these up into records which we then yield
        """
        streaming_reader = pa_csv.open_csv(
            file,
            pa.csv.ReadOptions(**self._read_options()),
            pa.csv.ParseOptions(**self._parse_options()),
            pa.csv.ConvertOptions(**self._convert_options(self._master_schema)),
        )
        still_reading = True
        while still_reading:
            try:
                batch = streaming_reader.read_next_batch()
            except StopIteration:
                still_reading = False
            else:
                batch_dict = batch.to_pydict()
                batch_columns = [col_info.name for col_info in batch.schema]
                # this gives us a list of lists where each nested list holds ordered values for a single column
                # e.g. [ [1,2,3], ["a", "b", "c"], [True, True, False] ]
                columnwise_record_values = [batch_dict[column] for column in batch_columns]
                # we zip this to get row-by-row, e.g. [ [1, "a", True], [2, "b", True], [3, "c", False] ]
                for record_values in zip(*columnwise_record_values):
                    # create our record of {col: value, col: value} by dict comprehension, iterating through all cols in batch_columns
                    yield {batch_columns[i]: record_values[i] for i in range(len(batch_columns))}

    def __read_stream_by_chunks(self, file: Union[TextIO, BinaryIO]) -> Iterator[Mapping[str, Any]]:
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.csv.open_csv.html
        PyArrow returns lists of values for each column so we zip() these up into records which we then yield
        """
        streaming_reader = pa_csv.open_csv(
            file,
            pa.csv.ReadOptions(**self._read_options()),
            pa.csv.ParseOptions(**self._parse_options()),
            pa.csv.ConvertOptions(**self._convert_options(self._master_schema)),
        )
        still_reading = True
        while still_reading:
            try:
                batch = streaming_reader.read_next_batch()
            except StopIteration:
                still_reading = False
            else:
                batch_dict = batch.to_pydict()
                batch_columns = [col_info.name for col_info in batch.schema]
                # this gives us a list of lists where each nested list holds ordered values for a single column
                # e.g. [ [1,2,3], ["a", "b", "c"], [True, True, False] ]
                columnwise_record_values = [batch_dict[column] for column in batch_columns]
                # we zip this to get row-by-row, e.g. [ [1, "a", True], [2, "b", True], [3, "c", False] ]
                for record_values in zip(*columnwise_record_values):
                    # create our record of {col: value, col: value} by dict comprehension, iterating through all cols in batch_columns
                    yield {batch_columns[i]: record_values[i] for i in range(len(batch_columns))}

    def stream_records(self, file: Union[TextIO, BinaryIO], file_info: FileInfo) -> Iterator[Mapping[str, Any]]:
        """
        Read and send data
        """
        yield from self.__read_stream_by_chunks(file)
        return
        if file_info.size < MAX_CHUNK_SIZE or not file_info.key.endswith(".csv"):
            yield from self.__read_stream_by_chunks(file)
            return
        self.logger.debug(f"The file '{file_info}' is large and try to load it by chunks")

        temp_file = os.path.join(TMP_FOLDER, "chunk.csv")
        try:
            for temp_descriptor in self.__create_chunk(temp_file, file):
                yield from self.__read_stream_by_chunks(temp_descriptor)
                temp_descriptor.close()
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    @classmethod
    def __find_line_end(cls, file: BinaryIO) -> Tuple[bytes, bytes]:
        """Tries to find a end of current line"""
        left_part = b""
        while True:
            chunk = file.read(1024)
            if not chunk:
                break
            left_part += chunk
            if len(left_part) > MAX_CHUNK_SIZE:
                raise Exception("incorrect CSV file because same line is more than 50Mb")

            found = left_part.find(b"\n")
            if found > -1:
                right_part = left_part[found:]
                return left_part[:found], right_part
        return b"", b""

    @classmethod
    def __create_chunk(cls, temp_file: str, file: BinaryIO) -> Iterator[BinaryIO]:
        chunk = None
        chunk_number = 1
        # select the first header line
        headers, tail_part = cls.__find_line_end(file)
        while True:
            try:
                # reuse a temporary file
                tf = open(temp_file, "wb")
                tf.write(headers)
                tf.write(tail_part)

                while os.stat(temp_file).st_size < MAX_CHUNK_SIZE:
                    chunk = file.read(1024 * 1024)
                    if not chunk:
                        break
                    tf.write(chunk)
                if chunk:
                    right_part, tail_part = cls.__find_line_end(file)
                    tf.write(right_part)
            finally:
                tf.close()
            chunk_size = os.stat(temp_file).st_size / 1024 ** 2
            cls.logger.debug(f"Chunk #{chunk_number} is created, size: {chunk_size} Mb")
            yield open(temp_file, "rb")
            if not chunk:
                break
            chunk_number += 1
