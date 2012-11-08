import functools
import os
import sys

import avro.ipc as ipc
import avro.protocol as protocol

# TODO(hammer): Figure the canonical place to put this file
PROTO_FILE = os.path.join(os.path.dirname(__file__), 'schema/hbase.avpr')
PROTOCOL = protocol.parse(open(PROTO_FILE).read())

def retry_wrapper(fn):
  """a decorator to add retry symantics to any method that uses hbase"""
  @functools.wraps(fn)
  def f(self, *args, **kwargs):
    try:
      return fn(self, *args, **kwargs)
    except:
      try:
        self.close()
      except:
        pass
      self.make_connection()
      return fn(self, *args, **kwargs)
  return f

class HBaseConnection(object):
  """
  Base class for HBase connections.  Supplies methods for a few basic
  queries and methods for cleanup of thrift results.
  """
  def __init__(self, host, port):
    self.host = host
    self.port = port
    self.client = None
    self.requestor = None
    self.make_connection()

  def make_connection(self, retry=2):
    """Establishes the underlying connection to HBase."""
    while retry:
      retry -= 1
      try:
        self.client = ipc.HTTPTransceiver(self.host, self.port)
        self.requestor = ipc.Requestor(PROTOCOL, self.client)
        return
      except:
        pass
    exceptionType, exception, tracebackInfo = sys.exc_info()
    raise exception

  # TODO(hammer): classify these methods like the HBase shell

  #
  # Metadata
  #

  @retry_wrapper
  def list_tables(self):
    """Grab table information."""
    return self.requestor.request("listTables", {})

  @retry_wrapper
  def get_hbase_version(self):
    """Get HBase version."""
    return self.requestor.request("getHBaseVersion", {})

  def get_cluster_status(self):
    """Get cluster status."""
    return self.requestor.request("getClusterStatus", {})

  @retry_wrapper
  def describe_table(self, table):
    """Grab table information."""
    return self.requestor.request("describeTable", {"table": table})

  @retry_wrapper
  def describe_family(self, table, family):
    """Grab family information."""
    return self.requestor.request("describeFamily", {"table": table, "family": family})

  @retry_wrapper
  def is_table_enabled(self, table):
    """Determine if a table is enabled."""
    return self.requestor.request("isTableEnabled", {"table": table})

  @retry_wrapper
  def table_exists(self, table):
    """Determine if a table exists."""
    return self.requestor.request("tableExists", {"table": table})

  #
  # Administrative Operations
  #

  @retry_wrapper
  def create_table(self, table, *families):
    table_descriptor = {"name": table}
    families = [{"name": family} for family in families]
    if families: table_descriptor["families"] = families
    return self.requestor.request("createTable", {"table": table_descriptor})

  # NB: Delete is an asynchronous operation, so don't retry
  def alter(self, table, command, family):
    self.disable_table(table)
    if command == "add":
      self.requestor.request("addFamily", {"table": table, "family": {"name": family}})
    elif command == "delete":
      self.requestor.request("deleteFamily", {"table": table, "family": family})
    else:
      return "Unknown alter command: %s" % command
    self.flush(".META.")
    return self.enable_table(table)

  # TODO(hammer): Automatically major_compact .META. too?
  # NB: flush is an asynchronous operation, so don't retry
  def drop(self, table):
    self.disable_table(table)
    self.requestor.request("deleteTable", {"table": table})
    return self.flush(".META.")

  def truncate(self, table):
    table_descriptor = self.describe_table(table)
    self.drop(table)
    return self.requestor.request("createTable", {"table": table_descriptor})

  # NB: flush is an asynchronous operation, so don't retry
  def flush(self, table):
    self.requestor.request("flush", {"table": table})

  # NB: split is an asynchronous operation, so don't retry
  def split(self, table):
    self.requestor.request("split", {"table": table})

  @retry_wrapper
  def enable_table(self, table):
    return self.requestor.request("enableTable", {"table": table})

  @retry_wrapper
  def disable_table(self, table):
    return self.requestor.request("disableTable", {"table": table})

  #
  # Get
  #

  # TODO(hammer): Figure out how to get binary keys
  # TODO(hammer): Do this parsing logic in pyhbase-cli?
  @retry_wrapper
  def get(self, table, row, *columns):
    get = {"row": row}
    columns = [len(column) > 1 and {"family": column[0], "qualifier": column[1]} or {"family": column[0]}
               for column in map(lambda s: s.split(":", 1), columns)]
    if columns: get["columns"] = columns
    params = {"table": table, "get": get}
    return self.requestor.request("get", params)

  @retry_wrapper
  def exists(self, table, row, *columns):
    get = {"row": row}
    columns = [len(column) > 1 and {"family": column[0], "qualifier": column[1]} or {"family": column[0]}
               for column in map(lambda s: s.split(":", 1), columns)]
    if columns: get["columns"] = columns
    params = {"table": table, "get": get}
    return self.requestor.request("exists", params)

  #
  # Put
  #

  # TODO(hammer): Figure out how to incorporate timestamps
  # TODO(hammer): Do this parsing logic in pyhbase-cli?
  @retry_wrapper
  def put(self, table, row, *column_values):
    put = {"row": row}
    column_values = [{"family": column.split(":", 1)[0], "qualifier": column.split(":", 1)[1], "value": value}
                     for column, value in zip(column_values[::2], column_values[1::2])]
    put["columnValues"] = column_values
    params = {"table": table, "put": put}
    return self.requestor.request("put", params)

  @retry_wrapper
  def incr(self, table, row, *column_and_amount):
    family, qualifier = column_and_amount[0].split(":", 1)
    amount = 1
    if len(column_and_amount) > 1:
      amount = column_and_amount[1]
    return self.requestor.request("incrementColumnValue", {"table": table, "row": row, "family": family, "qualifier": qualifier, "amount": int(amount), "writeToWAL": True})

  #
  # Delete
  #

  @retry_wrapper
  def delete(self, table, row, *columns):
    delete = {"row": row}
    columns = [len(column) > 1 and {"family": column[0], "qualifier": column[1]} or {"family": column[0]}
               for column in map(lambda s: s.split(":", 1), columns)]
    if columns: delete["columns"] = columns
    params = {"table": table, "delete": delete}
    return self.requestor.request("delete", params)

  #
  # Scan
  #

  # TODO(hammer): Figure out cleaner, more functional command-line
  @retry_wrapper
  def scan(self, table, number_of_rows, start_row=None,
           stop_row=None, columns=None, timestamp=None):

    if columns:
      columns = [{"family": column[0], "qualifier": column[1]}
                 if len(column) > 1 else {"family": column[0]}
                 for column in map(lambda s: s.split(":", 1), columns)]

    params = {
      "table": table,
      "scan": {
        "startRow": start_row,
        "stopRow": stop_row,
        "columns": columns,
        "timestamp": timestamp
        },
      }

    scanner_id = self.requestor.request("scannerOpen", params)

    results = self.requestor.request("scannerGetRows", {
        "scannerId": scanner_id,
        "numberOfRows": int(number_of_rows),
        })

    self.requestor.request("scannerClose", {"scannerId": scanner_id})
    return results
