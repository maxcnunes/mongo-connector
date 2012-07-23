"""
Module with code to setup cluster and test oplog_manager functions.
    
This is the main tester method. All the functions can be called by finaltests.py
"""

import subprocess
import sys
import time
import os
import json
import unittest

from setup_cluster import killMongoProc, startMongoProc, start_cluster 
from pymongo import Connection
from pymongo.errors import ConnectionFailure, OperationFailure
from os import path
from mongo_internal import Daemon
from threading import Timer
from oplog_manager import OplogThread
from solr_doc_manager import DocManager
from pysolr import Solr
from solr_simulator import SolrSimulator
from util import (long_to_bson_ts, 
                  bson_ts_to_long,
                  retry_until_ok)
from checkpoint import Checkpoint
from bson.objectid import ObjectId

""" Global path variables
"""    
PORTS_ONE = {"PRIMARY":"27117", "SECONDARY":"27118", "ARBITER":"27119", 
    "CONFIG":"27220", "MONGOS":"27217"}
PORTS_TWO = {"PRIMARY":"27317", "SECONDARY":"27318", "ARBITER":"27319", 
    "CONFIG":"27220", "MONGOS":"27217"}
SETUP_DIR = path.expanduser("~/mongo-connector")
DEMO_SERVER_DATA = SETUP_DIR + "/data"
DEMO_SERVER_LOG = SETUP_DIR + "/logs"
MONGOD_KSTR = " --dbpath " + DEMO_SERVER_DATA
MONGOS_KSTR = "mongos --port " + PORTS_ONE["MONGOS"]

def safe_mongo_op(func, arg1, arg2=None):
    while True:
        try:
            if arg2:
                func(arg1, arg2, safe=True)
            else:
                func(arg1, safe=True)
            break
        except OperationFailure:
            pass

#================================= #
#       Run Mongo* processes       #
#================================= #

    

class TestOplogManagerSharded(unittest.TestCase):
    """Defines all the testing methods, as well as a method that sets up the cluster
    """
    
    def runTest(self):
        unittest.TestCase.__init__(self)    
    
    def get_oplog_thread(self):
        """ Set up connection with mongo. Returns oplog, the connection and oplog collection
            
            This function clears the oplog
        """
        primary_conn = Connection('localhost', int(PORTS_ONE["PRIMARY"]))
        if primary_conn['admin'].command("isMaster")['ismaster'] is False:
            primary_conn = Connection('localhost', int(PORTS_ONE["SECONDARY"]))
        
        mongos_addr = "localhost:" + PORTS_ONE["MONGOS"]
        mongos_conn = Connection(mongos_addr)
        mongos_conn['alpha']['foo'].drop()
        
        oplog_coll = primary_conn['local']['oplog.rs']
        oplog_coll.drop()           # reset the oplog
        
        primary_conn['local'].create_collection('oplog.rs', capped=True, size=1000000)
        namespace_set = ['test.test', 'alpha.foo']
        doc_manager = SolrSimulator()
        oplog = OplogThread(primary_conn, mongos_addr, oplog_coll, True, doc_manager, {}, 
                            namespace_set, None)
        
        return (oplog, primary_conn, oplog_coll, mongos_conn)

    
    def get_oplog_thread_new(self):
        """ Set up connection with mongo. Returns oplog, the connection and oplog collection
            
            This function does not clear the oplog
        """
        primary_conn = Connection('localhost', int(PORTS_ONE["PRIMARY"]))
        if primary_conn['admin'].command("isMaster")['ismaster'] is False:
            primary_conn = Connection('localhost', int(PORTS_ONE["SECONDARY"]))

        mongos_conn = "localhost:" + PORTS_ONE["MONGOS"]
        oplog_coll = primary_conn['local']['oplog.rs']
        
        namespace_set = ['test.test', 'alpha.foo']
        doc_manager = SolrSimulator()
        oplog = OplogThread(primary_conn, mongos_conn, oplog_coll, True, doc_manager, {}, 
                            namespace_set, None)
        
        return (oplog, primary_conn, oplog_coll, oplog.mongos_connection)
        
    def test_retrieve_doc(self):
        """Test retrieve_doc in oplog_manager. Assertion failure if it doesn't pass
        """
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        #testing for entry as none type
        entry = None
        assert (test_oplog.retrieve_doc(entry) is None)
        
        oplog_cursor = oplog_coll.find({},tailable=True, await_data=True)
        
        assert (oplog_cursor.count() == 0)
        
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'})
        last_oplog_entry = oplog_cursor.next()
        target_entry = mongos_conn['alpha']['foo'].find_one()
        
        #testing for search after inserting a document
        assert (test_oplog.retrieve_doc(last_oplog_entry) == target_entry)
        
        safe_mongo_op(mongos_conn['alpha']['foo'].update, {'name':'paulie'}, {"$set": {'name':'paul'}})
        last_oplog_entry = oplog_cursor.next()
        target_entry = mongos_conn['alpha']['foo'].find_one()        
        
        #testing for search after updating a document
        assert (test_oplog.retrieve_doc(last_oplog_entry) == target_entry)
        
        safe_mongo_op(mongos_conn['alpha']['foo'].remove,{'name':'paul'})
        last_oplog_entry = oplog_cursor.next()
        
        #testing for search after deleting a document
        assert (test_oplog.retrieve_doc(last_oplog_entry) == None)
        
        last_oplog_entry['o']['_id'] = 'badID'
        
        #testing for bad doc id as input
        assert (test_oplog.retrieve_doc(last_oplog_entry) == None)
        
        test_oplog.stop()
        print 'PASSED TEST RETRIEVE DOC'
        
                
    def test_get_oplog_cursor(self):
        """Test get_oplog_cursor in oplog_manager. Assertion failure if it doesn't pass
        """
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        #test None cursor
        assert (test_oplog.get_oplog_cursor(None) == None)
        
        #test with one document
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'})
        ts = test_oplog.get_last_oplog_timestamp()
        cursor = test_oplog.get_oplog_cursor(ts)
        assert (cursor.count() == 1)
        
        #test with two documents, one after the ts
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paul'} )
        cursor = test_oplog.get_oplog_cursor(ts)
        assert (cursor.count() == 2)
        
        """ add test for when we're too behind the oplog
        #test case where timestamp is not in oplog which implies we're too behind 
        oplog_coll = primary_conn['local']['oplog.rs']
        oplog_coll.drop()           # reset the oplog
        primary_conn['local'].create_collection('oplog.rs', capped=True, size=10000)
        
        mongos_conn['alpha']['foo'].insert ( {'name':'pauline'} )
        assert (test_oplog.get_oplog_cursor(ts) == None)
        test_oplog.stop()
            
        #need to add tests for 'except' part of get_oplog_cursor
            """
        print 'PASSED TEST GET OPLOG CURSOR'

    def test_get_last_oplog_timestamp(self):
        """Test get_last_oplog_timestamp in oplog_manager. Assertion failure if it doesn't pass
        """
        
        #test empty oplog
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        
        assert (test_oplog.get_last_oplog_timestamp() == None)
        
        #test non-empty oplog
        oplog_cursor = oplog_coll.find({},tailable=True, await_data=True)
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'})
        last_oplog_entry = oplog_cursor.next()
        assert (test_oplog.get_last_oplog_timestamp() == last_oplog_entry['ts'])
        
        test_oplog.stop()
        
        print 'PASSED TEST GET OPLOG TIMESTAMP'
            
    def test_dump_collection(self):
        """Test dump_collection in oplog_manager. Assertion failure if it doesn't pass
        """
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        solr = SolrSimulator()
        test_oplog.doc_manager = solr
        
        #for empty oplog, no documents added
        assert (test_oplog.dump_collection(None) == None)
        assert (len(solr.test_search()) == 0)
        
        #with documents
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'} )
        search_ts = test_oplog.get_last_oplog_timestamp()
        test_oplog.dump_collection(search_ts)

        test_oplog.doc_manager.commit()
        solr_results = solr.test_search()
        assert (len(solr_results) == 1)
        solr_doc = solr_results[0]
        assert (long_to_bson_ts(solr_doc['_ts']) == search_ts)
        assert (solr_doc['name'] == 'paulie')
        assert (solr_doc['ns'] == 'alpha.foo')
        
        test_oplog.stop()
                
        print 'PASSED TEST DUMP COLLECTION'
        
    def test_init_cursor(self):
        """Test init_cursor in oplog_manager. Assertion failure if it doesn't pass
        """
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        test_oplog.checkpoint = Checkpoint()            #needed for these tests
        
        #initial tests with no config file and empty oplog
        assert (test_oplog.init_cursor() == None)
        
        #no config, single oplog entry
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'})
        search_ts = test_oplog.get_last_oplog_timestamp()
        cursor = test_oplog.init_cursor()
        
        
        assert (cursor.count() == 1)
        
        if test_oplog.checkpoint.commit_ts != search_ts:
            print 'test_oplog checkpoint ts is '
            print test_oplog.checkpoint.commit_ts
            print 'search ts is '
            print search_ts
                   
        assert (test_oplog.checkpoint.commit_ts == search_ts)
        
        #with config file, assert that size != 0
        os.system('touch temp_config.txt')
        
        #config_file_path = os.getcwd() + '/'+ 'temp_config.txt'
        #test_oplog.oplog_file = config_file_path
        cursor = test_oplog.init_cursor()
        #config_file_size = os.stat(config_file_path)[6]
        oplog_dict = test_oplog.oplog_progress_dict

        
        assert (cursor.count() == 1)
        self.assertTrue (test_oplog.oplog in oplog_dict)
        self.assertTrue (oplog_dict[test_oplog.oplog] == test_oplog.checkpoint.commit_ts)
        
        os.system('rm temp_config.txt')
        test_oplog.stop()
        print 'PASSED TEST INIT CURSOR'
    
    def test_prepare_for_sync(self):
        """Test prepare_for_sync in oplog_manager. Assertion failure if it doesn't pass
        """
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        cursor = test_oplog.prepare_for_sync()
        
        assert (test_oplog.checkpoint != None)
        assert (test_oplog.checkpoint.commit_ts == None)
        assert (cursor == None)
        
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'} )
        cursor = test_oplog.prepare_for_sync()
        search_ts = test_oplog.get_last_oplog_timestamp()

        #make sure that the cursor is valid and the timestamp is updated properly
        assert (test_oplog.checkpoint.commit_ts == search_ts)
        assert (cursor != None)
        assert (cursor.count() == 1)
        
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulter'} )
        cursor = test_oplog.prepare_for_sync()
        new_search_ts = test_oplog.get_last_oplog_timestamp()
        
        #make sure that the newest document is in the cursor.
        assert (cursor.count() == 2)
        next_doc = cursor.next()
        assert (next_doc['o']['name'] == 'paulter')
        assert (next_doc['ts'] == new_search_ts)

        test_oplog.stop()
        print 'PASSED TEST PREPARE FOR SYNC'
    
    """def test_write_config(self):
        #Test write_config in oplog_manager. Assertion failure if it doesn't pass
        
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        test_oplog.checkpoint = Checkpoint()
        
        
        #test that None is returned if there is no config file specified.
        assert (test_oplog.write_config() == None)
        
        #create config file
        os.system('touch temp_config.txt')
        config_file_path = os.getcwd() + '/temp_config.txt'
        test_oplog.oplog_file = config_file_path
        
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'name':'paulie'} )
        search_ts = test_oplog.get_last_oplog_timestamp()
        test_oplog.checkpoint.commit_ts = search_ts
        
        test_oplog.write_config()
        
        data = json.load(open(config_file_path, 'r'))
        oplog_str = str(oplog_coll.database.connection)
        
        assert (oplog_str in data[0])
        assert (search_ts == long_to_bson_ts(data[1]))
        
        #ensure that the temporary file is deleted
        assert (os.path.exists(config_file_path + '~') is False)
        
        search_ts = long_to_bson_ts(111111111111111)
        test_oplog.checkpoint.commit_ts = search_ts
        
        test_oplog.write_config()
        data = json.load(open(config_file_path, 'r'))
        oplog_str = str(oplog_coll.database.connection)
        
        assert (oplog_str in data[0])
        assert (search_ts == long_to_bson_ts(data[1]))
        
        test_oplog.stop()
        os.system('rm ' + config_file_path)
        
        print 'PASSED TEST WRITE CONFIG'
            
    def test_read_config(self):
        #Test read_config in oplog_manager. Assertion failure if it doesn't pass
        
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread()
        test_oplog.checkpoint = Checkpoint()
        
        #testing with no file
        assert(test_oplog.read_config() is None)
        #create config file
        os.system('touch temp_config.txt')
        config_file_path = os.getcwd() + '/temp_config.txt'
        test_oplog.oplog_file = config_file_path
        
        #testing with empty file
        assert(test_oplog.read_config() is None)
        
        #testing with a non-empty file
        search_ts = long_to_bson_ts(111111111111111)
        test_oplog.checkpoint.commit_ts = search_ts
        test_oplog.write_config()
        assert (test_oplog.read_config() == search_ts)

        #testing update
        search_ts = long_to_bson_ts(999999999999999)
        test_oplog.checkpoint.commit_ts = search_ts
        test_oplog.write_config()
        assert (test_oplog.read_config() == search_ts)
        os.system('rm ' + config_file_path)   
        
        print 'TEST READ CONFIG'
    """     

    def test_rollback(self):
        """Test rollback in oplog_manager. Assertion failure if it doesn't pass
        """
        
        os.system('rm config.txt; touch config.txt')
        start_cluster(sharded = True)
        
        test_oplog, primary_conn, oplog_coll, mongos_conn = self.get_oplog_thread_new()
        #test_oplog.start()
        
        solr = SolrSimulator()
        test_oplog.doc_manager = solr
        solr.test_delete()          #equivalent to solr.delete(q='*:*')
        obj1 = ObjectId('4ff74db3f646462b38000001')
        
        safe_mongo_op(mongos_conn['alpha']['foo'].remove, {})
        safe_mongo_op(mongos_conn['alpha']['foo'].insert, {'_id':obj1,'name':'paulie'})
        cutoff_ts = test_oplog.get_last_oplog_timestamp()
       
        obj2 = ObjectId('4ff74db3f646462b38000002')
        first_doc = {'name':'paulie', '_ts':bson_ts_to_long(cutoff_ts), 'ns':'alpha.foo', 
            '_id': obj1}
        
        #try kill one, try restarting
        killMongoProc(primary_conn.host, PORTS_ONE['PRIMARY'])
        
        new_primary_conn = Connection('localhost', int(PORTS_ONE['SECONDARY']))
        
        while new_primary_conn['admin'].command("isMaster")['ismaster'] is False:
            time.sleep(1)
        time.sleep(5)
        while True:
            try:
                mongos_conn['alpha']['foo'].insert, {'_id': obj2, 'name':'paul'}         
                break
            except: 
                time.sleep(1)
                continue
                
    
        killMongoProc(primary_conn.host, PORTS_ONE['SECONDARY'])  
                
        startMongoProc(PORTS_ONE['PRIMARY'], "demo-repl", "/replset1a", "/replset1a.log", None)

    
        #wait for master to be established
        while primary_conn['admin'].command("isMaster")['ismaster'] is False:
            time.sleep(1)

        startMongoProc(PORTS_ONE['SECONDARY'], "demo-repl", "/replset1b", "/replset1b.log", None)

                
        #wait for secondary to be established
        while new_primary_conn['admin'].command("replSetGetStatus")['myState'] != 2:
            time.sleep(1)
        
        while retry_until_ok(mongos_conn['alpha']['foo'].find().count) != 1:
            time.sleep(1)
            
            
        self.assertEqual (str(new_primary_conn.port), PORTS_ONE['SECONDARY'])
        self.assertEqual (str(primary_conn.port), PORTS_ONE['PRIMARY'])
       
        last_ts = test_oplog.get_last_oplog_timestamp()    
        second_doc = {'name':'paul', '_ts':bson_ts_to_long(last_ts), 'ns':'alpha.foo', '_id': obj2}
        
        test_oplog.doc_manager.upsert(first_doc)
        test_oplog.doc_manager.upsert(second_doc)
        test_oplog.rollback()
        test_oplog.doc_manager.commit()
        results = solr.test_search()
    
        self.assertEqual (len(results), 1)

        results_doc = results[0]
        self.assertEqual(results_doc['name'], 'paulie')
        self.assertTrue(results_doc['_ts'] <= bson_ts_to_long(cutoff_ts))
        
        print 'PASSED TEST ROLLBACK'

if __name__ == '__main__':
    os.system('rm config.txt; touch config.txt')
    start_cluster(sharded = True)
    conn = Connection('localhost:' + PORTS_ONE['MONGOS'])
    unittest.main()