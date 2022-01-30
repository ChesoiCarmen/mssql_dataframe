"""Rules for conversion between SQL, pandas, and odbc data types."""
import pandas as pd
import pyodbc

rules = pd.DataFrame.from_records(
    [
        {
            "sql_type": "bit",
            "sql_category": "boolean",
            "min_value": False,
            "max_value": True,
            "pandas_type": "boolean",
            "odbc_type": pyodbc.SQL_BIT,
            "odbc_size": 1,
            "odbc_precision": 0,
        },
        {
            "sql_type": "tinyint",
            "sql_category": "exact numeric",
            "min_value": 0,
            "max_value": 255,
            "pandas_type": "UInt8",
            "odbc_type": pyodbc.SQL_TINYINT,
            "odbc_size": 1,
            "odbc_precision": 0,
        },
        {
            "sql_type": "smallint",
            "sql_category": "exact numeric",
            "min_value": -(2 ** 15),
            "max_value": 2 ** 15 - 1,
            "pandas_type": "Int16",
            "odbc_type": pyodbc.SQL_SMALLINT,
            "odbc_size": 2,
            "odbc_precision": 0,
        },
        {
            "sql_type": "int",
            "sql_category": "exact numeric",
            "min_value": -(2 ** 31),
            "max_value": 2 ** 31 - 1,
            "pandas_type": "Int32",
            "odbc_type": pyodbc.SQL_INTEGER,
            "odbc_size": 4,
            "odbc_precision": 0,
        },
        {
            "sql_type": "bigint",
            "sql_category": "exact numeric",
            "min_value": -(2 ** 63),
            "max_value": 2 ** 63 - 1,
            "pandas_type": "Int64",
            "odbc_type": pyodbc.SQL_BIGINT,
            "odbc_size": 8,
            "odbc_precision": 0,
        },
        {
            "sql_type": "float",
            "sql_category": "approximate numeric",
            "min_value": -(1.79 ** 308),
            "max_value": 1.79 ** 308,
            "pandas_type": "float64",
            "odbc_type": pyodbc.SQL_FLOAT,
            "odbc_size": 8,
            "odbc_precision": 53,
        },
        {
            "sql_type": "time",
            "sql_category": "date time",
            "min_value": pd.Timedelta("00:00:00.0000000"),
            "max_value": pd.Timedelta("23:59:59.9999999"),
            "pandas_type": "timedelta64[ns]",
            "odbc_type": pyodbc.SQL_SS_TIME2,
            "odbc_size": 16,
            "odbc_precision": 7,
        },
        {
            "sql_type": "date",
            "sql_category": "date time",
            "min_value": pd.Timestamp((pd.Timestamp.min + pd.Timedelta(days=1)).date()),
            "max_value": pd.Timestamp(pd.Timestamp.max.date()),
            "pandas_type": "datetime64[ns]",
            "odbc_type": pyodbc.SQL_TYPE_DATE,
            "odbc_size": 10,
            "odbc_precision": 0,
        },
        {
            "sql_type": "datetime2",
            "sql_category": "date time",
            "min_value": pd.Timestamp.min,
            "max_value": pd.Timestamp.max,
            "pandas_type": "datetime64[ns]",
            "odbc_type": pyodbc.SQL_TYPE_TIMESTAMP,
            "odbc_size": 27,
            "odbc_precision": 7,
        },
        {
            "sql_type": "varchar",
            "sql_category": "character string",
            "min_value": 1,
            "max_value": 0,
            "pandas_type": "string",
            "odbc_type": pyodbc.SQL_VARCHAR,
            "odbc_size": 0,
            "odbc_precision": 0,
        },
        {
            "sql_type": "nvarchar",
            "sql_category": "character string",
            "min_value": 1,
            "max_value": 0,
            "pandas_type": "string",
            "odbc_type": pyodbc.SQL_WVARCHAR,
            "odbc_size": 0,
            "odbc_precision": 0,
        },
    ]
)
rules["sql_type"] = rules["sql_type"].astype("string")
rules["pandas_type"] = rules["pandas_type"].astype("string")
