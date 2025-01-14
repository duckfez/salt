"""
Return data to a mongodb server

Required python modules: pymongo


This returner will send data from the minions to a MongoDB server. MongoDB
server can be configured by using host, port, db, user and password settings
or by connection string URI (for pymongo > 2.3). To configure the settings
for your MongoDB server, add the following lines to the minion config files:

.. code-block:: yaml

    mongo.db: <database name>
    mongo.host: <server ip address>
    mongo.user: <MongoDB username>
    mongo.password: <MongoDB user password>
    mongo.port: 27017

Or single URI:

.. code-block:: yaml

   mongo.uri: URI

where uri is in the format:

.. code-block:: text

    mongodb://[username:password@]host1[:port1][,host2[:port2],...[,hostN[:portN]]][/[database][?options]]

Example:

.. code-block:: text

    mongodb://db1.example.net:27017/mydatabase
    mongodb://db1.example.net:27017,db2.example.net:2500/?replicaSet=test
    mongodb://db1.example.net:27017,db2.example.net:2500/?replicaSet=test&connectTimeoutMS=300000

More information on URI format can be found in
https://docs.mongodb.com/manual/reference/connection-string/

You can also ask for indexes creation on the most common used fields, which
should greatly improve performance. Indexes are not created by default.

.. code-block:: yaml

    mongo.indexes: true

Alternative configuration values can be used by prefacing the configuration.
Any values not found in the alternative configuration will be pulled from
the default location:

.. code-block:: yaml

    alternative.mongo.db: <database name>
    alternative.mongo.host: <server ip address>
    alternative.mongo.user: <MongoDB username>
    alternative.mongo.password: <MongoDB user password>
    alternative.mongo.port: 27017

Or single URI:

.. code-block:: yaml

   alternative.mongo.uri: URI

This mongo returner is being developed to replace the default mongodb returner
in the future and should not be considered API stable yet.

To use the mongo returner, append '--return mongo' to the salt command.

.. code-block:: bash

    salt '*' test.ping --return mongo

To use the alternative configuration, append '--return_config alternative' to the salt command.

.. versionadded:: 2015.5.0

.. code-block:: bash

    salt '*' test.ping --return mongo --return_config alternative

To override individual configuration items, append --return_kwargs '{"key:": "value"}' to the salt command.

.. versionadded:: 2016.3.0

.. code-block:: bash

    salt '*' test.ping --return mongo --return_kwargs '{"db": "another-salt"}'

"""

import logging

import salt.returners
import salt.utils.jid
from salt.utils.versions import LooseVersion as _LooseVersion

try:
    import pymongo

    PYMONGO_VERSION = _LooseVersion(pymongo.version)
    HAS_PYMONGO = True
except ImportError:
    HAS_PYMONGO = False

log = logging.getLogger(__name__)

# Define the module's virtual name
__virtualname__ = "mongo"


def __virtual__():
    if not HAS_PYMONGO:
        return False, "Could not import mongo returner; pymongo is not installed."
    return __virtualname__


def _remove_dots(src):
    """
    Remove the dots from the given data structure
    """
    output = {}
    for key, val in src.items():
        if isinstance(val, dict):
            val = _remove_dots(val)
        output[key.replace(".", "-")] = val
    return output


def _get_options(ret=None):
    """
    Get the mongo options from salt.
    """
    attrs = {
        "host": "host",
        "port": "port",
        "db": "db",
        "user": "user",
        "password": "password",
        "indexes": "indexes",
        "uri": "uri",
    }

    _options = salt.returners.get_returner_options(
        __virtualname__, ret, attrs, __salt__=__salt__, __opts__=__opts__
    )
    return _options


def _get_conn(ret):
    """
    Return a mongodb connection object
    """
    _options = _get_options(ret)

    host = _options.get("host")
    port = _options.get("port")
    uri = _options.get("uri")
    db_ = _options.get("db")
    user = _options.get("user")
    password = _options.get("password")
    indexes = _options.get("indexes", False)

    # at some point we should remove support for
    # pymongo versions < 2.3 until then there are
    # a bunch of these sections that need to be supported
    if uri and PYMONGO_VERSION > _LooseVersion("2.3"):
        if uri and host:
            raise salt.exceptions.SaltConfigurationError(
                "Mongo returner expects either uri or host configuration. Both were"
                " provided"
            )
        pymongo.uri_parser.parse_uri(uri)
        conn = pymongo.MongoClient(uri)
        mdb = conn.get_database()
    else:
        if PYMONGO_VERSION > _LooseVersion("2.3"):
            conn = pymongo.MongoClient(host, port, username=user, password=password)
        else:
            if uri:
                raise salt.exceptions.SaltConfigurationError(
                    "pymongo <= 2.3 does not support uri format"
                )
            conn = pymongo.Connection(host, port, username=user, password=password)

        mdb = conn[db_]

    if indexes:
        if PYMONGO_VERSION > _LooseVersion("2.3"):
            mdb.saltReturns.create_index("minion")
            mdb.saltReturns.create_index("jid")
            mdb.jobs.create_index("jid")
            mdb.events.create_index("tag")
        else:
            mdb.saltReturns.ensure_index("minion")
            mdb.saltReturns.ensure_index("jid")
            mdb.jobs.ensure_index("jid")
            mdb.events.ensure_index("tag")

    return conn, mdb


def returner(ret):
    """
    Return data to a mongodb server
    """
    conn, mdb = _get_conn(ret)

    if isinstance(ret["return"], dict):
        back = _remove_dots(ret["return"])
    else:
        back = ret["return"]

    if isinstance(ret, dict):
        full_ret = _remove_dots(ret)
    else:
        full_ret = ret

    log.debug(back)
    sdata = {
        "minion": ret["id"],
        "jid": ret["jid"],
        "return": back,
        "fun": ret["fun"],
        "full_ret": full_ret,
    }
    if "out" in ret:
        sdata["out"] = ret["out"]

    # save returns in the saltReturns collection in the json format:
    # { 'minion': <minion_name>, 'jid': <job_id>, 'return': <return info with dots removed>,
    #   'fun': <function>, 'full_ret': <unformatted return with dots removed>}
    #
    # again we run into the issue with deprecated code from previous versions

    if PYMONGO_VERSION > _LooseVersion("2.3"):
        # using .copy() to ensure that the original data is not changed, raising issue with pymongo team
        mdb.saltReturns.insert_one(sdata.copy())
    else:
        mdb.saltReturns.insert(sdata.copy())


def _safe_copy(dat):
    """ mongodb doesn't allow '.' in keys, but does allow unicode equivs.
        Apparently the docs suggest using escaped unicode full-width
        encodings.  *sigh*

            \\  -->  \\\\
            $  -->  \\\\u0024
            .  -->  \\\\u002e

        Personally, I prefer URL encodings,

        \\  -->  %5c
        $  -->  %24
        .  -->  %2e


        Which means also escaping '%':

        % -> %25
    """

    if isinstance(dat, dict):
        ret = {}
        for k in dat:
            r = (
                k.replace("%", "%25")
                .replace("\\", "%5c")
                .replace("$", "%24")
                .replace(".", "%2e")
            )
            if r != k:
                log.debug("converting dict key from %s to %s for mongodb", k, r)
            ret[r] = _safe_copy(dat[k])
        return ret

    if isinstance(dat, (list, tuple)):
        return [_safe_copy(i) for i in dat]

    return dat


def save_load(jid, load, minions=None):
    """
    Save the load for a given job id
    """
    conn, mdb = _get_conn(ret=None)
    to_save = _safe_copy(load)

    if PYMONGO_VERSION > _LooseVersion("2.3"):
        # using .copy() to ensure original data for load is unchanged
        mdb.jobs.insert_one(to_save)
    else:
        mdb.jobs.insert(to_save)


def save_minions(jid, minions, syndic_id=None):  # pylint: disable=unused-argument
    """
    Included for API consistency
    """


def get_load(jid):
    """
    Return the load associated with a given job id
    """
    conn, mdb = _get_conn(ret=None)
    return mdb.jobs.find_one({"jid": jid}, {"_id": 0})


def get_jid(jid):
    """
    Return the return information associated with a jid
    """
    conn, mdb = _get_conn(ret=None)
    ret = {}
    rdata = mdb.saltReturns.find({"jid": jid}, {"_id": 0})
    if rdata:
        for data in rdata:
            minion = data["minion"]
            # return data in the format {<minion>: { <unformatted full return data>}}
            ret[minion] = data["full_ret"]
    return ret


def get_fun(fun):
    """
    Return the most recent jobs that have executed the named function
    """
    conn, mdb = _get_conn(ret=None)
    ret = {}
    rdata = mdb.saltReturns.find_one({"fun": fun}, {"_id": 0})
    if rdata:
        ret = rdata
    return ret


def get_minions():
    """
    Return a list of minions
    """
    conn, mdb = _get_conn(ret=None)
    ret = []
    name = mdb.saltReturns.distinct("minion")
    ret.append(name)
    return ret


def get_jids():
    """
    Return a list of job ids
    """
    conn, mdb = _get_conn(ret=None)
    map = "function() { emit(this.jid, this); }"
    reduce = "function (key, values) { return values[0]; }"
    result = mdb.jobs.inline_map_reduce(map, reduce)
    ret = {}
    for r in result:
        jid = r["_id"]
        ret[jid] = salt.utils.jid.format_jid_instance(jid, r["value"])
    return ret


def prep_jid(nocache=False, passed_jid=None):  # pylint: disable=unused-argument
    """
    Do any work necessary to prepare a JID, including sending a custom id
    """
    return passed_jid if passed_jid is not None else salt.utils.jid.gen_jid(__opts__)


def event_return(events):
    """
    Return events to Mongodb server
    """
    conn, mdb = _get_conn(ret=None)

    if isinstance(events, list):
        events = events[0]

    if isinstance(events, dict):
        log.debug(events)

        if PYMONGO_VERSION > _LooseVersion("2.3"):
            mdb.events.insert_one(events.copy())
        else:
            mdb.events.insert(events.copy())
