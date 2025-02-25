"""Functions for data movement between Python pandas dataframes and SQL."""
import struct
from typing import Tuple, List
import logging
import pytz

import pyodbc
import numpy as np
import pandas as pd

from mssql_dataframe.core import (
    custom_errors,
    conversion_rules,
    dynamic,
)

logger = logging.getLogger(__name__)


def get_schema(
    connection: pyodbc.connect,
    table_name: str,
    dataframe: pd.DataFrame = None,
    additional_columns: List[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Get schema of an SQL table and the defined conversion rules between data types.

    If a dataframe is provided, also checks the contents of the dataframe for the ability
    to write to the SQL table and raises approriate exceptions if needed. Additionally
    converts the data types of the dataframe according to the conversion rules.

    Parameters
    ----------
    connection (pyodbc.connect) : connection to database
    table_name (str) : table name to read schema from
    dataframe (pandas.DataFrame, default=None) : check contents against schema and convert using rules
    additional_columns (list, default=None) : columns that will be generated by an SQL statement but not in the dataframe, such as metadata columns

    Returns
    -------
    schema (pandas.DataFrame) : table column specifications and conversion rules
    dataframe (pandas.DataFrame) : dataframe with contents converted to conform to SQL data type
    """
    cursor = connection.cursor()

    # add cataglog for temporary tables
    try:
        schema_name, table_name = table_name.split(".")
    except ValueError as err:
        if err.args[0].startswith("not enough values to unpack"):
            schema_name = None
        else:  # pragma: no cover
            raise
    if table_name.startswith("#"):
        catalog = "tempdb"
    else:
        catalog = None

    # get schema
    schema = []
    cursor = cursor.columns(table=table_name, catalog=catalog, schema=schema_name)
    for col in cursor:
        schema.append(list(col))
    schema = pd.DataFrame(schema, columns=[x[0] for x in cursor.description])
    # check for no SQL table
    if len(schema) == 0:
        raise custom_errors.SQLTableDoesNotExist(
            f"catalog = {catalog}, table_name = {table_name}, schema_name={schema_name}"
        )
    # check for missing columns not expected to be in dataframe
    # such as include_metadata_timestamps columns like _time_insert or _time_update
    # perform check seperately to insure this is raised without other dataframe columns
    if additional_columns is not None:
        columns = pd.Series(additional_columns, dtype="string")
        missing = columns[~columns.isin(schema["column_name"])]
        if len(missing) > 0:
            missing = list(missing)
            raise custom_errors.SQLColumnDoesNotExist(
                f"catalog = {catalog}, table_name = {table_name}, columns={missing}",
                missing,
            )
    # check for other missing columns
    if dataframe is not None:
        columns = dataframe.columns
        missing = columns[~columns.isin(schema["column_name"])]
        if len(missing) > 0:
            missing = list(missing)
            raise custom_errors.SQLColumnDoesNotExist(
                f"catalog = {catalog}, table_name = {table_name}, columns={missing}",
                missing,
            )
    # format schema
    schema = schema.rename(columns={"type_name": "sql_type"})
    schema = schema[
        [
            "column_name",
            "data_type",
            "column_size",
            "decimal_digits",
            "sql_type",
            "is_nullable",
            "ss_is_identity",
        ]
    ]
    schema[["column_name", "sql_type"]] = schema[["column_name", "sql_type"]].astype(
        "string"
    )
    schema["decimal_digits"] = schema["decimal_digits"].fillna(0).astype("int64")
    schema["is_nullable"] = schema["is_nullable"] == "YES"
    schema["ss_is_identity"] = schema["ss_is_identity"] == 1

    # add primary key info
    pk = cursor.primaryKeys(table=table_name, catalog=catalog).fetchall()
    pk = pd.DataFrame([list(x) for x in pk], columns=[x[0] for x in cursor.description])
    pk = pk.rename(columns={"key_seq": "pk_seq"})
    schema = schema.merge(
        pk[["column_name", "pk_seq", "pk_name"]],
        left_on="column_name",
        right_on="column_name",
        how="left",
    )
    schema["pk_seq"] = schema["pk_seq"].astype("Int64")
    schema["pk_name"] = schema["pk_name"].astype("string")

    # add conversion rules
    identity = schema["sql_type"] == "int identity"
    schema.loc[identity, "sql_type"] = "int"
    schema = schema.merge(
        conversion_rules.rules, left_on="sql_type", right_on="sql_type", how="left"
    )
    schema.loc[identity, "sql_type"] = "int identity"

    # key column_name as index, check for undefined conversion rule
    schema["column_name"] = schema["column_name"].astype("string")
    schema = schema.set_index(keys="column_name")
    missing = schema[conversion_rules.rules.columns].isna().any(axis="columns")
    if any(missing):
        missing = missing[missing].index.tolist()
        raise custom_errors.UndefinedConversionRule(
            "SQL data type conversion to pandas is not defined for columns:", missing
        )

    # check contents of dataframe against SQL schema & convert
    if dataframe is not None:
        dataframe = _precheck_dataframe(schema, dataframe)

    return schema, dataframe


def _precheck_dataframe(schema: pd.DataFrame, dataframe: pd.DataFrame) -> pd.DataFrame:
    """Check the contents of the dataframe for the ability to write to the SQL table.

    Raises approriate exceptions if needed. Additionally converts the data types of the
    dataframe according to the conversion rules.

    Parameters
    ----------
    schema (pandas.DataFrame) : contains definitions for data schema
    dataframe (pandas.DataFrame) : values to be written to SQL

    Returns
    -------
    dataframe (pandas.DataFrame) : converted according to SQL data type
    """
    # temporarily set dataframe index (primary key) as a column
    index = dataframe.index.names
    if any(index):
        dataframe = dataframe.reset_index()

    # only apply to columns in dataframe
    schema = schema[schema.index.isin(dataframe.columns)].copy()

    # convert objects to the largest sql_category type to allow for size check
    dataframe = convert_largest_sql_category(dataframe, schema)

    # check for insufficient column size, using min and max of dataframe contents
    check_column_size(dataframe, schema)

    # check if unicode to a nonunicode type
    check_unicode(dataframe, schema)

    # convert dataframe based on SQL type
    try:
        dataframe = dataframe.astype(schema["pandas_type"].to_dict())
    except TypeError:  # pragma: no cover
        raise custom_errors.DataframeColumnInvalidValue(
            "Dataframe columns cannot be converted based on their SQL data type"
        )

    # set primary key column as dataframe's index
    if any(schema["pk_seq"].notna()):
        pk = schema["pk_seq"].sort_values()
        pk = list(pk[pk.notna()].index)
        dataframe = dataframe.set_index(keys=pk)

    return dataframe


def convert_largest_sql_category(dataframe, schema):
    """Convert objects to allow for comparison without truncation."""
    # avoids downcast such as UInt8 value of 10000 to 16
    convert = dataframe.columns[dataframe.dtypes == "object"]
    try:
        # exact_whole_numeric
        columns = convert[schema.loc[convert, "sql_category"] == "exact_whole_numeric"]
        # BUG: first convert to float after replacing pandas.NA
        # https://github.com/pandas-dev/pandas/issues/25472
        dataframe[columns] = dataframe[columns].fillna(np.nan).replace([np.nan], [None])
        dataframe[columns] = dataframe[columns].astype("float")
        dataframe[columns] = dataframe[columns].astype("Int64")
        # approximate_decimal_numeric
        columns = convert[
            schema.loc[convert, "sql_category"] == "approximate_decimal_numeric"
        ]
        dataframe[columns] = dataframe[columns].astype("float64")
        # date_time
        columns = convert[
            (schema.loc[convert, "sql_category"] == "date_time")
            & (schema.loc[convert, "sql_type"] != "datetimeoffset")
        ]
        dataframe[columns] = dataframe[columns].astype("datetime64[ns]")
        # datetime offset
        columns = convert[
            (schema.loc[convert, "sql_category"] == "date_time")
            & (schema.loc[convert, "sql_type"] == "datetimeoffset")
        ]
        for col in columns:
            dataframe[col] = dataframe[col].apply(lambda x: pd.Timestamp(x))
        # character string
        columns = convert[schema.loc[convert, "sql_category"] == "character string"]
        dataframe[columns] = dataframe[columns].astype("string")
    except (TypeError, ValueError):  # pragma: no cover
        raise custom_errors.DataframeColumnInvalidValue(
            "Dataframe columns cannot be converted based on their SQL data type",
            list(columns),
        )

    return dataframe


def check_column_size(dataframe, schema):
    """Raise exception if dataframe value is too large for SQL data type specification."""
    check = dataframe.copy()
    strings = check.columns[check.dtypes == "string"]
    if any(strings):
        schema.loc[strings, "max_value"] = schema.loc[strings, "column_size"]
        check[strings] = check[strings].apply(lambda x: x.str.len())
    datetimeoffset = schema.index[schema["sql_type"] == "datetimeoffset"]
    standard = check.drop(columns=datetimeoffset)
    if len(standard.columns) == 0:  # pragma: no cover
        check = pd.DataFrame(columns=["min", "max"])
    else:
        check = standard.agg([min, max]).transpose()
    # calculate min/max for object pd.Timestamp seperately
    for col in datetimeoffset:
        check = pd.concat(
            [
                check,
                pd.DataFrame(
                    {"min": min(dataframe[col]), "max": max(dataframe[col])},
                    index=[col],
                ),
            ]
        )
    check = check.merge(
        schema[["min_value", "max_value"]], left_index=True, right_index=True
    )
    try:
        invalid = check[
            (check["min"] < check["min_value"]) | (check["max"] > check["max_value"])
        ]
    except TypeError:  # pragma: no cover
        raise custom_errors.DataframeColumnInvalidValue(
            "Dataframe columns cannot be converted based on their SQL data type"
        )

    if len(invalid) > 0:
        invalid = invalid.astype("string")
        invalid["allowed"] = invalid["min_value"] + " to " + invalid["max_value"]
        invalid["actual"] = invalid["min"] + " to " + invalid["max"]
        columns = list(invalid.index)
        raise custom_errors.SQLInsufficientColumnSize(
            f"columns: {columns}, allowed range: {list(invalid.allowed)}, actual range: {list(invalid.actual)}",
            columns,
        )


def contains_unicode(series: pd.Series):
    """Determine if a string contains unicode.

    Parameters
    ----------
    series (pandas.Series) : data to check

    Return
    ------
    check (bool) : True if series contains unicode

    """
    pre = series.str.len()
    post = series.str.encode("ascii", errors="ignore").str.len().astype("Int64")
    # check if encodidng removes characeters
    check = pre.ne(post).any()

    return check


def check_unicode(dataframe, schema):
    """Raise error if string contains unicode for SQL char/varchar column."""
    columns = schema[schema["sql_type"].isin(["char", "varchar"])].index
    for col in columns:
        if contains_unicode(dataframe[col]):
            raise custom_errors.SQLNonUnicodeTypeColumn


def prepare_cursor(
    schema: pd.DataFrame, dataframe: pd.DataFrame, cursor: pyodbc.connect
) -> pyodbc.connect:
    """Prepare cursor data types and size for writting values to SQL.

    Parameters
    ----------
    schema (pandas.DataFrame) : output from get_schema function
    dataframe (pandas.DataFrame) : values to be written to SQL, used to determine size of string columns
    cursor (pyodbc.connect.cursor) : cursor to be used to write values

    Returns
    -------
    cursor (pyodbc.connect.cursor) : cursor with SQL data type and size parameters set
    """
    schema = schema[
        [
            "column_size",
            "decimal_digits",
            "min_value",
            "max_value",
            "sql_category",
            "sql_type",
            "odbc_type",
        ]
    ]

    # insure columns are sorted correctly
    columns = list(dataframe.columns)
    index = dataframe.index.names
    if any(index):
        columns = list(index) + columns
    schema = schema.loc[columns]

    # set SQL data type and size for cursor
    schema = schema[["odbc_type", "column_size", "decimal_digits"]].to_numpy().tolist()
    schema = [tuple(x) for x in schema]
    cursor.setinputsizes(schema)

    return cursor


def prepare_time(schema, prepped, dataframe):
    """Prepare time for writting to SQL."""
    dtype = schema[schema["sql_type"] == "time"].index

    invalid = (
        (prepped[dtype] >= pd.Timedelta(days=1))
        | (prepped[dtype] < pd.Timedelta(days=0))
    ).any()
    if any(invalid):
        invalid = list(invalid[invalid].index)
        raise ValueError(
            f"columns {invalid} are out of range for SQL TIME data type. Allowable range is 00:00:00.0000000-23:59:59.9999999"
        )

    if any(dtype):
        truncation = prepped[dtype].apply(lambda x: any(x.dt.nanoseconds % 100 > 0))
        truncation = list(truncation[truncation].index)
    else:
        truncation = []
    if any(truncation):
        msg = f"Nanosecond precision for dataframe columns {truncation} will be rounded as SQL data type 'time' allows 7 max decimal places."
        logger.warning(msg)
    # round nanosecond to the 7th decimal place ...123456789 -> ...123456800 for SQL
    for col in truncation:
        rounded = dataframe[col].apply(
            lambda x: pd.Timedelta(
                days=x.components.days,
                hours=x.components.hours,
                minutes=x.components.minutes,
                seconds=x.components.seconds,
                microseconds=x.components.microseconds,
                nanoseconds=round(x.components.nanoseconds / 100) * 100,
            )
            if pd.notnull(x)
            else x
        )
        dataframe[col] = rounded
        prepped[col] = rounded
    if any(dtype):
        # convert to string since python datetime.time allows 6 decimal places but SQL allows 7
        prepped[dtype] = prepped[dtype].astype("str")
        prepped[dtype] = prepped[dtype].replace({"NaT": None})
        prepped[dtype] = prepped[dtype].apply(lambda x: x.str[7:23])

    return prepped, dataframe


def prepare_datetime(schema, prepped, dataframe):
    """Prepare datetime for writting to SQL."""
    dtype = schema[schema["sql_type"] == "datetime"].index
    if any(dtype):
        adjust = prepped[dtype].apply(lambda x: any(x.dt.microsecond % 3000 > 0))
    else:
        adjust = []
    if any(adjust):
        adjust = list(adjust[adjust].index)
        msg = f"Millisecond precision for dataframe columns {adjust} will be rounded as SQL data type 'datetime' rounds to increments of .000, .003, or .007 seconds."
        logger.warning(msg)
        # round millisecond to the 3rd decimal place in approriate increments ...008 -> ..007 for SQL
        increments = np.array([10, 7, 3, 0]).reshape(-1, 1)
        for col in adjust:
            thousandths = (
                (prepped[col].dt.microsecond / 1000 % 10).to_numpy().reshape(1, -1)
            )
            thousandths = increments[np.abs(thousandths - increments).argmin(axis=0)]

            milliseconds = prepped[col].dt.microsecond // 10000 * 10 + pd.Series(
                thousandths[:, 0], index=prepped.index
            )
            milliseconds = pd.to_timedelta(milliseconds, unit="milliseconds")

            rounded = dataframe[col].dt.floor("s") + milliseconds

            dataframe[col] = rounded
            prepped[col] = rounded

    if any(dtype):
        # convert to string since python datetime.datetime allows 6 decimals but SQL allows 7
        prepped[dtype] = prepped[dtype].astype("str")
        prepped[dtype] = prepped[dtype].replace({"NaT": None})
        prepped[dtype] = prepped[dtype].apply(lambda x: x.str[0:27])

    return prepped, dataframe


def prepare_datetime2(schema, prepped, dataframe):
    """Prepare datetime2 for writting to SQL."""
    dtype = schema[schema["sql_type"] == "datetime2"].index

    if any(dtype):
        truncation = prepped[dtype].apply(lambda x: any(x.dt.nanosecond % 100 > 0))
    else:
        truncation = []
    if any(truncation):
        truncation = list(truncation[truncation].index)
        msg = f"Nanosecond precision for dataframe columns {truncation} will be rounded as SQL data type 'datetime2' allows 7 max decimal places."
        logger.warning(msg)
        # round nanosecond to the 7th decimal place ...145224193 -> ...145224200 for SQL
        for col in truncation:
            rounded = dataframe[col].apply(
                lambda x: pd.Timestamp(
                    x.year,
                    x.month,
                    x.day,
                    x.hour,
                    x.minute,
                    x.second,
                    x.microsecond,
                    round(x.nanosecond / 100) * 100,
                )
                if pd.notnull(x)
                else x
            )
            rounded = rounded.astype("datetime64[ns]")
            dataframe[col] = rounded
            prepped[col] = rounded
    if any(dtype):
        # convret to string since python datetime.datetime allows 6 decimals but SQL allows 7
        prepped[dtype] = prepped[dtype].astype("str")
        prepped[dtype] = prepped[dtype].replace({"NaT": None})
        prepped[dtype] = prepped[dtype].apply(lambda x: x.str[0:27])

    return prepped, dataframe


def prepare_datetimeoffset(schema, prepped, dataframe):
    """Prepare datetimeoffset for writing to SQL."""
    dtype = schema[schema["sql_type"] == "datetimeoffset"].index
    truncation = pd.Series(dtype="object")
    for col in dtype:
        # replace None with pd.NaT
        dataframe[col] = dataframe[col].fillna(pd.NaT)
        # assume +00:00 UTC if time zone is not set
        dataframe[col] = dataframe[col].apply(
            lambda x: x.tz_localize("UTC") if x.tzinfo is None else x
        )
        # apply adjustments to prepped data that will be inserted
        prepped[col] = dataframe[col]
        # check if pandas datatype has greater precision than SQL data type
        # TODO: check need to round/truncate timezoneoffset?
        extra = prepped[col].apply(lambda x: x.nanosecond % 100 > 0).any()
        truncation = pd.concat([truncation, pd.Series(extra, index=[col])])

    if any(truncation):
        truncation = list(truncation[truncation].index)
        msg = f"Nanosecond precision for dataframe columns {truncation} will be rounded as SQL data type 'datetimeoffset' allows 7 max decimal places."
        logger.warning(msg)
        # round nanosecond to the 7th decimal place ...145224193 -> ...145224200 for SQL
        for col in truncation:
            rounded = (
                dataframe[col]
                .apply(
                    lambda x: pd.Timestamp(
                        year=x.year,
                        month=x.month,
                        day=x.day,
                        hour=x.hour,
                        minute=x.minute,
                        second=x.second,
                        microsecond=x.microsecond,
                        nanosecond=round(x.nanosecond / 100) * 100,
                        tzinfo=x.tzinfo,
                    )
                    if pd.notnull(x)
                    else x
                )
                .fillna(pd.NaT)
            )
            dataframe[col] = rounded
            prepped[col] = rounded
    if any(dtype):
        # convert to string since python datetime.datetime allows 6 decimals but SQL allows 7
        # string is also needed to represent time zone offset
        for col in dtype:
            prepped[col] = prepped[col].astype("str")
            prepped[col] = prepped[col].replace({"NaT": None})
            # limit to 7 decimal places
            prepped[col] = prepped[col].str.replace(r"(?<=\.\d{7})00", "", regex=True)
            # include +00:00 where tzinfo is None
            prepped[col] = prepped[col].str.replace(
                r"(?<=\.\d{7})$", r"\g<0>+00:00", regex=True
            )

    return prepped, dataframe


def prepare_numeric(schema, prepped, dataframe):
    """Prepare numeric & decimal for writting to SQL."""
    dtype = schema[schema["sql_type"].isin(["numeric", "decimal"])].index
    for col in dtype:
        # set a common missing value
        dataframe[col] = dataframe[col].replace([pd.NA], None)
        # round to correct number of decimal digits
        decimal_digits = int(schema.at[col, "decimal_digits"])
        prepped[col] = dataframe[col].apply(
            lambda x: round(x, decimal_digits) if pd.notna(x) else x
        )
        prepped[col] = prepped[col].astype(dataframe[col].dtype)
        if not dataframe[col].equals(prepped[col]):
            msg = f"Decimal digits for column [{col}] will be rounded to {decimal_digits} decimal places to fit SQL specification for this column."
            logger.warning(msg)
        dataframe[col] = prepped[col]

    return prepped, dataframe


def prepare_values(
    schema: pd.DataFrame, dataframe: pd.DataFrame
) -> Tuple[pd.DataFrame, list]:
    """Prepare dataframe contents for writting values to SQL.

    Parameters
    ----------
    schema (pandas.DataFrame) : data schema definition
    dataframe (pandas.DataFrame) : dataframe that will be written to SQL

    Returns
    -------
    dataframe (pandas.DataFrame) : values that may be altered to conform to SQL precision limitations
    values (list) : values to pass to pyodbc.connect.cursor.executemany

    """
    # create a copy to preserve values in return
    prepped = dataframe.copy()

    # include index as column as it is the primary key
    # also retain the origional index for dataframe/series comparisons
    index = prepped.index.names
    if any(index):
        prepped = prepped.reset_index()
        prepped = prepped.set_index(index, drop=False)
        dataframe = dataframe.reset_index()
        dataframe = dataframe.set_index(index, drop=False)

    # only prepare values currently in dataframe
    schema = schema[schema.index.isin(prepped.columns)]

    # round and truncate values to be the same as SQL
    prepped, dataframe = prepare_time(schema, prepped, dataframe)
    prepped, dataframe = prepare_datetime(schema, prepped, dataframe)
    prepped, dataframe = prepare_datetime2(schema, prepped, dataframe)
    prepped, dataframe = prepare_datetimeoffset(schema, prepped, dataframe)
    prepped, dataframe = prepare_numeric(schema, prepped, dataframe)

    # reset the index temporarily set as columns for preparing values
    if any(index):
        dataframe = dataframe.set_index(index)

    # treat pandas NA,NaT,etc as NULL in SQL
    # prepped = prepped.fillna(np.nan).replace([np.nan], [None])

    # # convert single column of datetime to objects
    # # otherwise tolist() will produce ints instead of datetimes
    # if prepped.shape[1] == 1 and prepped.select_dtypes("datetime").shape[1] == 1:
    #     prepped = prepped.astype(object)

    # BUG: treat pandas NA,NaT,etc as NULL in SQL
    # ideally we woudn't want to convert everything to an object
    prepped = prepped.astype(object)
    prepped = prepped.where(pd.notnull(prepped), None)

    # values for pyodbc cursor executemany
    values = prepped.values.tolist()

    return dataframe, values


def convert_time(connection):
    """
    Convert SQL time to timedelta.

    pyodbc "SQL_SS_TIME2" = T-SQL "TIME"

    python datetime.time has 6 decimal places of precision and isn't nullable
    pandas Timedelta supports 9 decimal places and is nullable
    SQL TIME only supports 7 decimal places for precision
    SQL TIME range is '00:00:00.0000000' to '23:59:59.9999999' while pandas allows multiple days and negatives
    """

    def SQL_SS_TIME2(raw_bytes, pattern=struct.Struct("<4hI")):
        hour, minute, second, _, fraction = pattern.unpack(raw_bytes)
        return pd.Timedelta(
            hours=hour,
            minutes=minute,
            seconds=second,
            microseconds=fraction // 1000,
            nanoseconds=fraction % 1000,
        )

    connection.add_output_converter(pyodbc.SQL_SS_TIME2, SQL_SS_TIME2)

    return connection


def convert_timestamp(connection):
    """
    Convert SQL DATETIME2/DATETIME to datetime.

    Types: pyodbc "SQL_TYPE_TIMESTAMP" = T-SQL "DATETIME2" or pyodbc "SQL_TYPE_TIMESTAMP" =  T-SQL "DATETIME"
    python datetime.datetime has 6 decimal places of precision and isn't nullable
    pandas Timestamp supports 9 decimal places and is nullable
    SQL DATETIME2 only supports 7 decimal places for precision
    SQL DATETIME only supports 3 decimal places for precision in rounded increments of .000, .003, or .007 seconds
    pandas Timestamp range range is '1677-09-21 00:12:43.145225' to '2262-04-11 23:47:16.854775807'
    DATETIME2 allows '0001-01-01' through '9999-12-31'
    DATETIME allows '1753-01-01' through '9999-12-31'
    """

    def SQL_TYPE_TIMESTAMP(raw_bytes):
        # DATETIME2 (16 bytes)
        if len(raw_bytes) == 16:
            pattern = struct.Struct("hHHHHHI")
            year, month, day, hour, minute, second, fraction = pattern.unpack(raw_bytes)
            timestamp = pd.Timestamp(
                year=year,
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                second=second,
                microsecond=fraction // 1000,
                nanosecond=fraction % 1000,
            )
        # DATETIME (8 bytes)
        else:
            pattern = struct.Struct("iI")
            days, ticks = pattern.unpack(raw_bytes)
            timestamp = pd.Timestamp(year=1900, month=1, day=1) + pd.Timedelta(
                days=days, milliseconds=round(3.33333333 * ticks)
            )

        return timestamp

    connection.add_output_converter(pyodbc.SQL_TYPE_TIMESTAMP, SQL_TYPE_TIMESTAMP)

    return connection


def convert_datetimeoffset(connection):
    """
    Convert SQL datetimeoffset to datetime with timezone.

    Types: pyodbc "UNDEFINED" = T-SQL "DATETIMEOFFSET" = ODBC SQL type "-155"
    """

    def SQL_TYPE_DATETIMEOFFSET(raw_bytes):
        (
            year,
            month,
            day,
            hour,
            minute,
            second,
            fraction,
            offset_hour,
            offset_minute,
        ) = struct.unpack("<6hI2h", raw_bytes)

        timestamp = pd.Timestamp(
            year=year,
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            second=second,
            microsecond=fraction // 1000,
            nanosecond=fraction % 1000,
            tzinfo=pytz.FixedOffset(offset_hour * 60 + offset_minute),
        )

        return timestamp

    connection.add_output_converter(-155, SQL_TYPE_DATETIMEOFFSET)

    return connection


def prepare_connection(connection: pyodbc.connect) -> pyodbc.connect:
    """Prepare connection by adding output converters for data types directly to a pandas data type.

    1. Avoids errors such as pyodbc.ProgrammingError where the ODBC library doesn't already have a conversion defined.
    pyodbc.ProgrammingError: ('ODBC SQL type -155 is not yet supported. column-index=0 type=-155', 'HY106')

    2. Conversion directly to a pandas types allows greater precision. Python datetime.datetime allows 6
    decimal places of precision while pandas Timestamps allows 9.

    Note that adding converters for nullable pandas integer types isn't possible, since those are implemented at the
    array level. Pandas also doesn't support an exact precision decimal data type.

    Parameters
    ----------
    connection (pyodbc.connect) : connection without default output converters

    Returns
    -------
    connection (pyodbc.connect) : connection with added output converters
    """
    connection = convert_time(connection)
    connection = convert_timestamp(connection)
    connection = convert_datetimeoffset(connection)

    return connection


def insert_values(
    table_name: str,
    dataframe: pd.DataFrame,
    include_metadata_timestamps: bool,
    schema: pd.DataFrame,
    cursor: pyodbc.connect,
):
    """Insert values from a dataframe into an SQL table.

    Parameters
    ----------
    table_name (str) : name of table to insert data into
    dataframe (pandas.DataFrame): tabular data to insert
    schema (pandas.DataFrame) : properties of SQL table columns where data will be inserted
    include_metadata_timestamps (bool) : include _time_insert column
    cursor (pyodbc.connect.cursor) : cursor to be used to write values

    Returns
    -------
    dataframe (pandas.DataFrame) : values that may be altered to conform to SQL precision limitations
    """
    # column names from dataframe contents
    if any(dataframe.index.names):
        # named index columns will also have values returned from conversion.prepare_values
        columns = list(dataframe.index.names) + list(dataframe.columns)
    else:
        columns = dataframe.columns

    # dynamic SQL object names
    table = dynamic.escape(cursor, table_name)
    columns = dynamic.escape(cursor, columns)

    # prepare values of dataframe for insert
    dataframe, values = prepare_values(schema, dataframe)

    # prepare cursor for input data types and sizes
    cursor = prepare_cursor(schema, dataframe, cursor)

    # issue insert statement
    if include_metadata_timestamps:
        insert = "_time_insert, " + ", ".join(columns)
        params = "GETDATE(), " + ", ".join(["?"] * len(columns))
    else:
        insert = ", ".join(columns)
        params = ", ".join(["?"] * len(columns))

    # skip security check since table and columns have been escaped
    statement = f"""
    INSERT INTO
    {table} (
        {insert}
    ) VALUES (
        {params}
    )
    """  # nosec hardcoded_sql_expressions
    cursor.executemany(statement, values)
    cursor.commit()

    # values that may be altered to conform to SQL precision limitations
    return dataframe


def read_values(
    statement: str, schema: pd.DataFrame, connection: pyodbc.connect, args: list = None
) -> pd.DataFrame:
    """Read data from SQL into a pandas dataframe.

    Parameters
    ----------
    statement (str) : statement to execute to get data
    schema (pandas.DataFrame) : output from get_schema function for setting dataframe data types
    connection (pyodbc.connect) : connection to database
    args (list, default=None) : arguments to pass for parameter placeholders

    Returns
    -------
    result (pandas.DataFrame) : resulting data from performing statement
    """
    # add output converters
    connection = prepare_connection(connection)

    # create cursor to fetch data
    cursor = connection.cursor()

    # read data from SQL
    if args is None:
        result = cursor.execute(statement).fetchall()
    else:
        result = cursor.execute(statement, *args).fetchall()
    columns = pd.Series([col[0] for col in cursor.description])

    # form output using SQL schema and explicit pandas types
    if any(~columns.isin(schema.index)):
        columns = list(columns[~columns.isin(schema.index)])
        raise AttributeError(f"missing columns from schema: {columns}")
    dtypes = schema.loc[columns, "pandas_type"].to_dict()
    result = {col: [row[idx] for row in result] for idx, col in enumerate(columns)}
    result = {col: pd.Series(vals, dtype=dtypes[col]) for col, vals in result.items()}
    result = pd.DataFrame(result)

    # replace missing values in object columns with pandas type
    datetimeoffset = schema.index[schema["sql_type"] == "datetimeoffset"]
    result[datetimeoffset] = result[datetimeoffset].fillna(pd.NaT)

    # set primary key columns as index
    keys = list(schema[schema["pk_seq"].notna()].index)
    if keys:
        try:
            result = result.set_index(keys=keys)
        except KeyError:
            raise KeyError(f"primary key column missing from query: {keys}")

    return result
