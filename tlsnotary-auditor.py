#!/usr/bin/env python
from __future__ import print_function

import BaseHTTPServer
import base64
import binascii
import hashlib
import hmac
import os
import platform
import Queue
import re
import shutil
import SimpleHTTPServer
import socket
from SocketServer import ThreadingMixIn
import struct
import subprocess
import sys
import tarfile
import threading
import time
import random
import urllib2
import zipfile

installdir = os.path.dirname(os.path.realpath(__file__))
datadir = os.path.join(installdir, 'auditor')
sessionsdir = os.path.join(datadir, 'sessions')

platform = platform.system()
if platform == 'Windows':
    OS = 'mswin'
elif platform == 'Linux':
    OS = 'linux'
elif platform == 'Darwin':
    OS = 'macos'

#exit codes
MINIHTTPD_FAILURE = 2
MINIHTTPD_WRONG_RESPONSE = 3
MINIHTTPD_START_TIMEOUT = 4
FIREFOX_MISSING= 1
BROWSER_START_ERROR = 5
BROWSER_NOT_FOUND = 6
WRONG_HASH = 8


sslkeylogfile = ''
current_sessiondir = ''
browser_exepath = 'firefox'

IRCsocket = socket._socketobject
my_nick = ''
auditee_nick = ''
channel_name = '#tlsnotary'
myPrivateKey = auditeePublicKey = None

google_modulus = 0
google_exponent = 0
PMS_second_half = ''

recvQueue = Queue.Queue() #all IRC messages are placed on this queue
ackQueue = Queue.Queue() #count_my_messages_thread places messages' ordinal numbers on this thread 
progressQueue = Queue.Queue() #messages intended to be displayed by the frontend are placed here

secretbytes_amount=8
bTerminateAllThreads = False

def bigint_to_bytearray(bigint):
    m_bytes = []
    while bigint != 0:
        b = bigint%256
        m_bytes.insert( 0, b )
        bigint //= 256
    return bytearray(m_bytes)

#processes each http request in a separate thread
#we need threading in order to send progress updates to the frontend in a non-blocking manner
class StoppableThreadedHttpServer (ThreadingMixIn, BaseHTTPServer.HTTPServer):
    """http server that reacts to self.stop flag"""
    retval = ''
    def serve_forever (self):
        """Handle one request at a time until stopped. Optionally return a value"""
        self.stop = False
        self.socket.setblocking(1)
        while not self.stop:
                self.handle_request()
        return self.retval;
    

#look at tshark's ascii dump (option '-x') to better understand the parsing taking place
def get_html_from_asciidump(ascii_dump):
    hexdigits = set('0123456789abcdefABCDEF')
    binary_html = bytearray()
    if ascii_dump == '':
        print ('empty frame dump',end='\r\n')
        return -1
    #We are interested in
    # "Uncompressed entity body" for compressed HTML (both chunked and not chunked). If not present, then
    # "De-chunked entity body" for no-compression, chunked HTML. If not present, then
    # "Reassembled SSL" for no-compression no-chunks HTML in multiple SSL segments, If not present, then
    # "Decrypted SSL data" for no-compression no-chunks HTML in a single SSL segment.
    uncompr_pos = ascii_dump.rfind('Uncompressed entity body')
    if uncompr_pos != -1:
        for line in ascii_dump[uncompr_pos:].split('\n')[1:]:
            #convert ascii representation of hex into binary so long as first 4 chars are hexdigits
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                #if first 4 chars are not hexdigits, we reached the end of the section
                break
        return binary_html    
    #else ------------------------------------------------------------------------------------------------------------#
    dechunked_pos = ascii_dump.rfind('De-chunked entity body')
    if dechunked_pos != -1:
        for line in ascii_dump[dechunked_pos:].split('\n')[1:]:
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                break
        return binary_html          
    #else ------------------------------------------------------------------------------------------------------------#
    reassembled_pos = ascii_dump.rfind('Reassembled SSL')
    if reassembled_pos != -1:
        for line in ascii_dump[reassembled_pos:].split('\n')[1:]:
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                #http HEADER is delimited from HTTP body with '\r\n\r\n'
                if binary_html.find('\r\n\r\n') == -1:
                    return -1
                break
        return binary_html.split('\r\n\r\n', 1)[1]
    #else ------------------------------------------------------------------------------------------------------------#
    decrypted_pos = ascii_dump.rfind('Decrypted SSL data')
    if decrypted_pos != -1:       
        for line in ascii_dump[decrypted_pos:].split('\n')[1:]:
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                #http HEADER is delimited from HTTP body with '\r\n\r\n'
                if binary_html.find('\r\n\r\n') == -1:
                    return -1
                break
        return binary_html.split('\r\n\r\n', 1)[1]    
    

#respond to PING messages and put all the other messages onto the recvQueue
def receivingThread():
    if not hasattr(receivingThread, "last_seq_which_i_acked"):
        receivingThread.last_seq_which_i_acked = 0 #static variable. Initialized only on first function's run
     
    first_chunk='' #we put the first chunk here and do a new loop iteration to pick up the second one
    second_chunk=''
    while True:
        buffer = ''
        try: buffer = IRCsocket.recv(1024)
        except: continue #1 sec timeout
        if not buffer: continue
        messages = buffer.split('\r\n')  #sometimes the server packs multiple PRIVMSGs into one message separated with \r\n
        for onemsg in messages:            
            msg = onemsg.split()
            if len(msg)==0 : continue  #stray newline
            if msg[0] == "PING":
                IRCsocket.send("PONG %s" % msg[1]) #answer with pong as per RFC 1459
                continue
            #check if the message is correctly formatted
            if not len(msg) >= 5: continue
            if not (msg[1]=='PRIVMSG' and msg[2]==channel_name and msg[3]==':'+my_nick ): continue
            exclamaitionMarkPosition = msg[0].find('!')
            nick_from_message = msg[0][1:exclamaitionMarkPosition]
            if not auditee_nick == nick_from_message: continue
            print ('RECEIVED: ' + buffer)
            if len(msg)==5 and msg[4].startswith('ack:'):
                ackQueue.put(msg[4][len('ack:'):])
                continue
            if not (len(msg)==7 and msg[4].startswith('seq:')): continue
            his_seq = int(msg[4][len('seq:'):])
            if his_seq <= receivingThread.last_seq_which_i_acked: 
                #the other side is out of sync, send an ack again
                IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditee_nick + ' ack:' + str(his_seq) + ' \r\n')
                continue
            if not his_seq == receivingThread.last_seq_which_i_acked+1: continue #we did not receive the next seq in order
            #else we got a new seq      
            if first_chunk=='' and  not msg[5].startswith( ('cr_sr_hmac_n_e', 'gcr_gsr', 'verify_md5sha:', 'zipsig:', 'link:', 'commit_hash:') ) : continue         
            #check if this is the first chunk of a chunked message. Only 2 chunks are supported for now
            #'CRLF' is used at the end of the first chunk, 'EOL' is used to show that there are no more chunks
            if msg[-1]=='CRLF':
                if first_chunk != '': 
                    if second_chunk != '': #we already have two chunks, no more are allowed
                        continue
                    else:
                        second_chunk = msg[5]
                else:
                    first_chunk = msg[5]
                IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditee_nick + ' ack:' + str(his_seq) + ' \r\n')
                receivingThread.last_seq_which_i_acked = his_seq
                continue #go pickup another chunk
            elif msg[-1]=='EOL' and first_chunk != '': #last chunk arrived
                print ('second chunk arrived')
                assembled_message = first_chunk + second_chunk + msg[5]
                recvQueue.put(assembled_message)
                first_chunk='' #empty the container for the next iterations
                second_chunk=''
                IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditee_nick + ' ack:' + str(his_seq) + ' \r\n')
                receivingThread.last_seq_which_i_acked = his_seq
            elif msg[-1]=='EOL':
                recvQueue.put(msg[5])
                IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditee_nick + ' ack:' + str(his_seq) + ' \r\n')
                receivingThread.last_seq_which_i_acked = his_seq
                

def send_message(data):
    if not hasattr(send_message, "my_seq"):
        send_message.my_seq = 100000 #static variable. Initialized only on first function's run

    #empty queue from possible leftovers
    #try: ackQueue.get_nowait()
    #except: pass
    #split up data longer than chunk_size bytes (IRC message limit is 512 bytes including the header data)
    #'\r\n' must go to the end of each message
    chunk_size=350    
    chunks = len(data)/chunk_size + 1
    if len(data)%chunk_size == 0: chunks -= 1 #avoid creating an empty chunk if data length is a multiple of chunk_size
    
    for chunk_index in range(chunks) :
        send_message.my_seq += 1
        chunk = data[chunk_size*chunk_index:chunk_size*(chunk_index+1)]
        for i in range (3):
            bWasMessageAcked = False
            ending = ' EOL ' if chunk_index+1==chunks else ' CRLF ' #EOL for the last chunk, otherwise CRLF
            irc_msg = 'PRIVMSG ' + channel_name + ' :' + auditee_nick + ' seq:' + str(send_message.my_seq) + ' ' + chunk + ending +' \r\n'
            #empty the ack queue. Not using while True: because sometimes an endless loop would happen TODO: find out why
            for j in range(5):
                try: ackQueue.get_nowait()
                except: pass
            bytessent = IRCsocket.send(irc_msg)
            print('SENT: ' + str(bytessent) + ' ' + irc_msg)                
            try: ack_check = ackQueue.get(block=True, timeout=3)
            except: continue #send again because ack was not received
            if not str(send_message.my_seq) == ack_check: continue
            #else: correct ack received
            bWasMessageAcked = True
            break
        if not bWasMessageAcked:
            return ('failure',)
    return('success',)


    
#Receive messages from auditee, perform calculations, and respond to them accordingly
def process_messages():
    global auditee_nick
    global PMS_second_half
    #after the auditee was authorized, entering a regular message processing loop
    while True:
        try:
            msg = recvQueue.get(block=True, timeout=1)
        except: continue
        print ('got msg ' + str(msg) + ' from recvQueue')

        if msg.startswith('gcr_gsr:'): #the first msg must be 'gcr_gsr'
            b64_gcr_gsr = msg[len('gcr_gsr:'):]
            try: gcr_gsr = base64.b64decode(b64_gcr_gsr)
            except:
                print ('base64 decode error in cr_gcr_gsr')
                continue               
            google_cr = gcr_gsr[:32]
            google_sr = gcr_gsr[32:64]
            
            PMS_second_half =  os.urandom(secretbytes_amount) + ('\x00' * (24-secretbytes_amount-1)) + '\x01'
            RSA_PMS_google_int = pow( int(('\x01'+('\x00'*25)+PMS_second_half).encode('hex'),16), google_exponent, google_modulus )
            grsapms = bigint_to_bytearray(RSA_PMS_google_int)
            #-------------------BEGIN get sha1hmac for google
            label = "master secret"
            seed = google_cr + google_sr        
            #start the PRF
            sha1A1 = hmac.new(PMS_second_half,  label+seed, hashlib.sha1).digest()
            sha1A2 = hmac.new(PMS_second_half,  sha1A1, hashlib.sha1).digest()
            sha1A3 = hmac.new(PMS_second_half,  sha1A2, hashlib.sha1).digest()
            
            sha1hmac1 = hmac.new(PMS_second_half, sha1A1 + label + seed, hashlib.sha1).digest()
            sha1hmac2 = hmac.new(PMS_second_half, sha1A2 + label + seed, hashlib.sha1).digest()
            sha1hmac3 = hmac.new(PMS_second_half, sha1A3 + label + seed, hashlib.sha1).digest()
            ghmac = (sha1hmac1+sha1hmac2+sha1hmac3)[:48]
            #-------------------END get sha1hmac for google
            
            b64_grsapms_ghmac = base64.b64encode(grsapms+ghmac)
            send_message('grsapms_ghmac:'+ b64_grsapms_ghmac)
            continue
         #------------------------------------------------------------------------------------------------------#   
        elif msg.startswith('cr_sr_hmac_n_e:'): #the first msg must be 'cr_sr_hmac_n_e_gcr_gsr'
            progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': Processing data from the auditee.')
            b64_cr_sr_hmac_n_e = msg[len('cr_sr_hmac_n_e:'):]
            try: cr_sr_hmac_n_e = base64.b64decode(b64_cr_sr_hmac_n_e)
            except:
                print ('base64 decode error in cr_sr_hmac_n_e')
                continue
            
            cipher_suite_int = int(cr_sr_hmac_n_e[:1].encode('hex'), 16)
            if cipher_suite_int == 4: cipher_suite = 'RC4MD5'
            elif cipher_suite_int == 5: cipher_suite = 'RC4SHA'
            elif cipher_suite_int == 47: cipher_suite = 'AES128'
            elif cipher_suite_int == 53: cipher_suite = 'AES256'
            else: raise Exception ('invalid cipher sute')

            cr = cr_sr_hmac_n_e[1:33]
            sr = cr_sr_hmac_n_e[33:65]
            md5hmac_for_MS_first_half=cr_sr_hmac_n_e[65:89] #half of MS's 48 bytes
            n = cr_sr_hmac_n_e[89:345]
            e = cr_sr_hmac_n_e[345:348]
            n_int = int(n.encode('hex'),16)
            e_int = int(e.encode('hex'),16)
                        
            #RSA encryption without padding: ciphertext = plaintext^e mod n
            RSA_PMS_second_half_int = pow( int(('\x01'+('\x00'*25)+PMS_second_half).encode('hex'),16), e_int, n_int )
            
            label = "master secret"
            seed = cr + sr        
            #start the PRF
            sha1A1 = hmac.new(PMS_second_half,  label+seed, hashlib.sha1).digest()
            sha1A2 = hmac.new(PMS_second_half,  sha1A1, hashlib.sha1).digest()
            sha1A3 = hmac.new(PMS_second_half,  sha1A2, hashlib.sha1).digest()
            
            sha1hmac1 = hmac.new(PMS_second_half, sha1A1 + label + seed, hashlib.sha1).digest()
            sha1hmac2 = hmac.new(PMS_second_half, sha1A2 + label + seed, hashlib.sha1).digest()
            sha1hmac3 = hmac.new(PMS_second_half, sha1A3 + label + seed, hashlib.sha1).digest()
            sha1hmac = (sha1hmac1+sha1hmac2+sha1hmac3)[:48]

            sha1hmac_for_MS_first_half = sha1hmac[:24]
            sha1hmac_for_MS_second_half = sha1hmac[24:48]
            
            MS_first_half = bytearray([ord(a) ^ ord(b) for a,b in zip(md5hmac_for_MS_first_half, sha1hmac_for_MS_first_half)])
                   
            #master secret key expansion
            #see RFC2246 6.3. Key calculation & 5. HMAC and the pseudorandom function
            #The amount of key material for each ciphersuite:
            #AES256-CBC-SHA: mac key 20*2, encryption key 32*2, IV 16*2 == 136bytes
            #AES128-CBC-SHA: mac key 20*2, encryption key 16*2, IV 16*2 == 104bytes
            #RC4128_MD5: mac key 16*2, encryption key 16*2 == 64 bytes
            #RC4128_SHA: mac key 20*2, encryption key 16*2 == 72bytes

            #Regardless of theciphersuite, we generate the max key material we'd ever need which is 136 bytes
            label = "key expansion"
            seed = sr + cr
            #this is not optimized in a loop on purpose. I want people to see exactly what is going on
            md5A1 = hmac.new(MS_first_half,  label+seed, hashlib.md5).digest()
            md5A2 = hmac.new(MS_first_half,  md5A1, hashlib.md5).digest()
            md5A3 = hmac.new(MS_first_half,  md5A2, hashlib.md5).digest()
            md5A4 = hmac.new(MS_first_half,  md5A3, hashlib.md5).digest()
            md5A5 = hmac.new(MS_first_half,  md5A4, hashlib.md5).digest()
            md5A6 = hmac.new(MS_first_half,  md5A5, hashlib.md5).digest()
            md5A7 = hmac.new(MS_first_half,  md5A6, hashlib.md5).digest()
            md5A8 = hmac.new(MS_first_half,  md5A7, hashlib.md5).digest()
            md5A9 = hmac.new(MS_first_half,  md5A8, hashlib.md5).digest()
            
            md5hmac1 = hmac.new(MS_first_half, md5A1 + label + seed, hashlib.md5).digest()
            md5hmac2 = hmac.new(MS_first_half, md5A2 + label + seed, hashlib.md5).digest()
            md5hmac3 = hmac.new(MS_first_half, md5A3 + label + seed, hashlib.md5).digest()
            md5hmac4 = hmac.new(MS_first_half, md5A4 + label + seed, hashlib.md5).digest()
            md5hmac5 = hmac.new(MS_first_half, md5A5 + label + seed, hashlib.md5).digest()
            md5hmac6 = hmac.new(MS_first_half, md5A6 + label + seed, hashlib.md5).digest()
            md5hmac7 = hmac.new(MS_first_half, md5A7 + label + seed, hashlib.md5).digest()
            md5hmac8 = hmac.new(MS_first_half, md5A8 + label + seed, hashlib.md5).digest()
            md5hmac9 = hmac.new(MS_first_half, md5A9 + label + seed, hashlib.md5).digest()
            
            md5hmac = (md5hmac1+md5hmac2+md5hmac3+md5hmac4+md5hmac5+md5hmac6+md5hmac7+md5hmac8+md5hmac9)
            #fill the place of server MAC with zeroes
            if cipher_suite == 'AES256': 
                md5hmac_for_ek = md5hmac[:20] + bytearray(os.urandom(20)) + md5hmac[40:136]
            elif cipher_suite == 'AES128':
                md5hmac_for_ek = md5hmac[:20] + bytearray(os.urandom(20)) + md5hmac[40:104]
            elif cipher_suite == 'RC4SHA':
                md5hmac_for_ek = md5hmac[:20] + bytearray(os.urandom(20)) + md5hmac[40:72]
            elif cipher_suite == 'RC4MD5': 
                md5hmac_for_ek = md5hmac[:16] + bytearray(os.urandom(16)) + md5hmac[32:64]
        
            rsapms_hmacms_hmacek = bigint_to_bytearray(RSA_PMS_second_half_int)+sha1hmac_for_MS_second_half+md5hmac_for_ek
            b64_rsapms_hmacms_hmacek = base64.b64encode(rsapms_hmacms_hmacek)
            send_message('rsapms_hmacms_hmacek:'+ b64_rsapms_hmacms_hmacek)
            continue
        #------------------------------------------------------------------------------------------------------#    
        elif msg.startswith('verify_md5sha:'):
            b64_md5sha = msg[len('verify_md5sha:') : ]
            try: md5sha = base64.b64decode(b64_md5sha)
            except:
                print ('base64 decode error in verify_md5sha')
                continue
            md5 = md5sha[:16] #md5 hash is 16bytes
            sha = md5sha[16:]   #sha hash is 20 bytes
            
            #calculate verify_data for Finished message
            #see RFC2246 7.4.9. Finished & 5. HMAC and the pseudorandom function
            label = "client finished"
            seed = md5 + sha
           
            md5A1 = hmac.new(MS_first_half,  label+seed, hashlib.md5).digest()
            md5hmac1 = hmac.new(MS_first_half, md5A1 + label + seed, hashlib.md5).digest()
            b64_verify_hmac = base64.b64encode(md5hmac1)
            send_message('verify_hmac:'+b64_verify_hmac)
            continue
        #------------------------------------------------------------------------------------------------------#    
        elif msg.startswith('commit_hash:'):
            b64_commit_hash = msg[len('commit_hash:'):]
            try: commit_hash = base64.b64decode(b64_commit_hash)
            except: 
                print ('base64 decode error in commit_hash')
                continue
            trace_hash = commit_hash[:32]
            md5hmac_hash = commit_hash[32:64]
            commit_dir = os.path.join(current_sessiondir, 'commit')
            if not os.path.exists(commit_dir): os.makedirs(commit_dir)
            #file names are assigned sequentially hash1, hash2 etc.
            #The auditee must provide tracefiles trace1, trace2 corresponding to these sequence numbers
            last_seqno = 0
            commdir_list = os.listdir(commit_dir)
            for one_trace in commdir_list:
                if not one_trace.startswith('tracehash'): continue
                this_seqno = int( one_trace[len('tracehash'):] )
                if not this_seqno > last_seqno: continue
                last_seqno = this_seqno
                continue
            my_seqno = last_seqno+1
            trace_hash_path = os.path.join(commit_dir, 'tracehash'+str(my_seqno))
            md5hmac_hash_path =  os.path.join(commit_dir, 'md5hmac_hash'+str(my_seqno))
            with open(trace_hash_path, 'wb') as f: f.write(trace_hash)
            with open(md5hmac_hash_path, 'wb') as f: f.write(md5hmac_hash)
            sha1hmac_path = os.path.join(commit_dir, 'sha1hmac'+str(my_seqno))
            with open(sha1hmac_path, 'wb') as f: f.write(sha1hmac)
            cr_path = os.path.join(commit_dir, 'cr'+str(my_seqno))
            with open(cr_path, 'wb') as f: f.write(cr)
            b64_sha1hmac = base64.b64encode(sha1hmac) 
            send_message('sha1hmac_for_MS:'+b64_sha1hmac)
            continue  
        #------------------------------------------------------------------------------------------------------#           
        elif msg.startswith('link:'):
            b64_link = msg[len('link:'):]
            try: link = base64.b64decode(b64_link)
            except:
                print ('base64 decode error in link')
                continue
            time.sleep(3) #just in case the server needs some time to process the file
            req = urllib2.Request(link)
            resp = urllib2.urlopen(req)
            linkdata = resp.read()
            with open(os.path.join(current_sessiondir, 'auditeetrace.zip'), 'wb') as f : f.write(linkdata)
            zipf = zipfile.ZipFile(os.path.join(current_sessiondir, 'auditeetrace.zip'), 'r')
            auditeetrace_dir = os.path.join(current_sessiondir, 'auditeetrace')
            zipf.extractall(auditeetrace_dir)
            response = 'success' #unless overridden by a failure in sanity check
            #sanity: all trace names must be unique and their hashes must correspond to the
            #hashes which the auditee committed to earlier
            adir_list = os.listdir(auditeetrace_dir)
            seqnos = []
            for one_trace in adir_list:
                if not one_trace.startswith('trace'): continue
                try: this_seqno = int(one_trace[len('trace'):])
                except:
                    print ('WARNING: Could not cast trace\'s tail to int')
                    response = 'failure'
                    break
                if this_seqno in seqnos:
                    print ('WARNING: multiple tracefiles names detected')
                    response = 'failure'
                    break
                saved_hash_path = os.path.join(commit_dir, 'tracehash'+str(this_seqno))
                if not os.path.exists(saved_hash_path):
                    print ('WARNING: Auditee gave a trace number which doesn\'t have a committed hash')
                    response = 'failure'
                    break
                with open(saved_hash_path, 'rb') as f: saved_hash = f.read()
                with open(os.path.join(auditeetrace_dir, one_trace), 'rb') as f: tracedata = f.read()
                trace_hash = hashlib.sha256(tracedata).digest()
                if not saved_hash == trace_hash:
                    print ('WARNING: Trace\'s hash doesn\'t match the hash committed to')
                    response = 'failure'
                    break
                md5hmac_path = os.path.join(auditeetrace_dir, 'md5hmac'+str(this_seqno))
                if not os.path.exists(md5hmac_path):
                    print ('WARNING: Could not find md5hmac in auditeetrace')
                    response = 'failure'
                    break
                with open(md5hmac_path, 'rb') as f: md5hmac_data = f.read()
                md5hmac_hash = hashlib.sha256(md5hmac_data).digest()
                with open(os.path.join(commit_dir, 'md5hmac_hash'+str(this_seqno)), 'rb') as f: commited_md5hmac_hash = f.read()
                if not md5hmac_hash == commited_md5hmac_hash:
                    print ('WARNING: mismatch in committed md5hmac hashes')
                    response = 'failure'
                    break
                #elif no errors
                seqnos.append(this_seqno)
                continue
            send_message('response:'+response)
            if response == 'success':
                progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': The auditee has successfully finished the audit session')
            else:
                progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': WARNING!!! The auditee FAILED the audit session')
            progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': Decrypting  auditee\'s data')

            #decrypt  the tracefiles
            decr_dir = os.path.join(current_sessiondir, 'decrypted')
            os.makedirs(decr_dir)
            for one_trace in adir_list:
                if not one_trace.startswith('trace'): continue
                seqno = one_trace[len('trace'):]
                with open(os.path.join(auditeetrace_dir, 'md5hmac'+seqno)) as f: md5hmac = f.read()
                with open(os.path.join(commit_dir, 'sha1hmac'+seqno)) as f: sha1hmac = f.read()
                with open(os.path.join(commit_dir, 'cr'+seqno)) as f: cr = f.read()
                ms = bytearray( [ord(a) ^ ord(b) for a,b in zip(md5hmac, sha1hmac)] )#xoring
                sslkeylog = os.path.join(decr_dir, 'sslkeylog'+seqno)
                cr_hexl = binascii.hexlify(cr)
                ms_hexl = binascii.hexlify(ms)
                skl_fd = open(sslkeylog, 'wb')
                skl_fd.write('CLIENT_RANDOM ' + cr_hexl + ' ' + ms_hexl + '\n')
                skl_fd.close()
                output = subprocess.check_output(['tshark', '-r', os.path.join(auditeetrace_dir, one_trace), '-Y', 'ssl and http.content_type contains html', '-o', 'http.ssl.port:1025-65535', '-o', 'ssl.keylog_file:'+ sslkeylog, '-x'])
                if output == '': raise Exception ("Failed to find HTML in escrowtrace")
                #output may contain multiple frames with HTML, we examine them one-by-one
                separator = re.compile('Frame ' + re.escape('(') + '[0-9]{2,7} bytes' + re.escape(')') + ':')
                #ignore the first split element which is always an empty string
                frames = re.split(separator, output)[1:]    
                html_paths = ''
                for index,oneframe in enumerate(frames):
                    html = get_html_from_asciidump(oneframe)
                    path = os.path.join(decr_dir, 'html-'+seqno+'-'+str(index))
                    with open(path, 'wb') as f: f.write(html)
            progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': All decrypted HTML can be found in ' + decr_dir)
            progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': You may now close the browser.')
            continue

      
#Receive HTTP HEAD requests from FF extension. This is how the extension communicates with python backend.
class Handler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    #Using HTTP/1.0 instead of HTTP/1.1 is crucial, otherwise the minihttpd just keep hanging
    #https://mail.python.org/pipermail/python-list/2013-April/645128.html
    protocol_version = "HTTP/1.0"      
    
    def do_HEAD(self):
        global myPrivateKey
        global auditeePublicKey
        
        print ('minihttp received ' + self.path + ' request',end='\r\n')
        # example HEAD string "/page_marked?accno=12435678&sum=1234.56&time=1383389835"    
        # we need to adhere to CORS and add extra headers in server replies
        if self.path.startswith('/get_recent_keys'):
            #this is the very first command that we expect in a new session.
            #If this is the very first time tlsnotary is run, there will be no saved keys
            #otherwise we load up the saved keys which the user can override with new keys if need be
            my_privkey_pem = my_pubkey_pem = auditee_pubkey_pem = ''
            if os.path.exists(os.path.join(datadir, 'recentkeys')):
                if os.path.exists(os.path.join(datadir, 'recentkeys', 'myprivkey')) and os.path.exists(os.path.join(datadir, 'recentkeys', 'mypubkey')):
                    with open(os.path.join(datadir, 'recentkeys', 'myprivkey'), 'r') as f: my_privkey_pem = f.read()
                    with open(os.path.join(datadir, 'recentkeys', 'mypubkey'), 'r') as f: my_pubkey_pem = f.read()
                    with open(os.path.join(current_sessiondir, 'myprivkey'), 'w') as f: f.write(my_privkey_pem)
                    with open(os.path.join(current_sessiondir, 'mypubkey'), 'w') as f: f.write(my_pubkey_pem)
                    myPrivateKey = rsa.PrivateKey.load_pkcs1(my_privkey_pem)
                if os.path.exists(os.path.join(datadir, 'recentkeys', 'auditeepubkey')):
                    with open(os.path.join(datadir, 'recentkeys', 'auditeepubkey'), 'r') as f: auditee_pubkey_pem = f.read()
                    with open(os.path.join(current_sessiondir, 'auditorpubkey'), 'w') as f: f.write(auditee_pubkey_pem)
                    auditeePublicKey = rsa.PublicKey.load_pkcs1(auditee_pubkey_pem)
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response, mypubkey, auditeepubkey")
            self.send_header("response", "get_recent_keys")
            #if pem keys were empty '' then slicing[:] will produce an empty string ''
            #Esthetical step: cut off the standard header and footer to make keys look smaller replacing newlines wth dashes
            my_pubkey_pem_stub = my_pubkey_pem[40:-38].replace('\n', '_')
            auditee_pubkey_pem_stub = auditee_pubkey_pem[40:-38].replace('\n', '_')
            self.send_header("mypubkey", my_pubkey_pem_stub)
            self.send_header("auditeepubkey", auditee_pubkey_pem_stub)
            self.end_headers()
            return
             
             
        if self.path.startswith('/new_keypair'):            
            pubkey, privkey = rsa.newkeys(1024)
            myPrivateKey = privkey
            my_pubkey_pem = pubkey.save_pkcs1()
            my_privkey_pem = privkey.save_pkcs1()
            #------------------------------------------
            with open(os.path.join(current_sessiondir, 'myprivkey'), 'w') as f: f.write(my_privkey_pem)
            with open(os.path.join(current_sessiondir, 'mypubkey'), 'w') as f: f.write(my_pubkey_pem)
            #also save the keys as recent, so that they could be reused in the next session
            if not os.path.exists(os.path.join(datadir, 'recentkeys')): os.makedirs(os.path.join(datadir, 'recentkeys'))
            with open(os.path.join(datadir, 'recentkeys' , 'myprivkey'), 'w') as f: f.write(my_privkey_pem)
            with open(os.path.join(datadir, 'recentkeys', 'mypubkey'), 'w') as f: f.write(my_pubkey_pem)
            #---------------------------------------------
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response, pubkey")
            self.send_header("response", "new_keypair")
            my_pubkey_pem_stub = my_pubkey_pem[40:-38].replace('\n', '_')
            self.send_header("pubkey", my_pubkey_pem_stub)
            self.end_headers()
            return

        if self.path.startswith('/import_auditee_pubkey'):
            arg_str = self.path.split('?', 1)[1]
            if not arg_str.startswith('pubkey='):
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Expose-Headers", "response, status")
                self.send_header("response", "import_auditee_pubkey")
                self.send_header("status", 'wrong HEAD parameter')
                self.end_headers()
                return
            #elif HEAD parameters were OK
            auditee_pubkey_pem_stub = arg_str[len('pubkey='):]
            auditee_pubkey_pem_stub = auditee_pubkey_pem_stub.replace('_', '\n')
            auditee_pubkey_pem = '-----BEGIN RSA PUBLIC KEY-----\nMIGJAoGBA' + auditee_pubkey_pem_stub + 'AgMBAAE=\n-----END RSA PUBLIC KEY-----\n'
            auditeePublicKey = rsa.PublicKey.load_pkcs1(auditee_pubkey_pem)
            with open(os.path.join(current_sessiondir, 'auditeepubkey'), 'w') as f: f.write(auditee_pubkey_pem)
            #also save the key as recent, so that they could be reused in the next session
            if not os.path.exists(os.path.join(datadir, 'recentkeys')): os.makedirs(os.path.join(datadir, 'recentkeys'))
            with open(os.path.join(datadir, 'recentkeys' , 'auditeepubkey'), 'w') as f: f.write(auditee_pubkey_pem)
            #-----------------------------------------
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response, status")
            self.send_header("response", "import_auditee_pubkey")
            self.send_header("status", 'success')
            self.end_headers()
            return
        
        if self.path.startswith('/start_irc'):
            #connect to IRC send hello to the auditor and get a hello in return
            rv = start_irc()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response, status")
            self.send_header("response", "start_irc")
            self.send_header("status", rv)
            self.end_headers()
            return
        
        
        if self.path.startswith('/progress_update'):
            #receive this command in a loop, blocking for 30 seconds until there is something to respond with
            update = 'no update'
            time_started = int(time.time())
            while int(time.time()) - time_started < 30:
                try: 
                    update = progressQueue.get(block=False)
                    break #something in the queue
                except:
                    if bTerminateAllThreads: break
                    time.sleep(1) #nothing in the queue
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response, update")
            self.send_header("response", "progress_update")
            self.send_header("update", update)
            self.end_headers()
            return
        
        
        if self.path.startswith('/terminate'):
            rv = 'terminate()'
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response, status")
            self.send_header("response", "terminate")
            self.send_header("status", rv)
            self.end_headers()
            return   
        else:
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "response")
            self.send_header("response", "unknown command")
            self.end_headers()
            return
    
      
def registerAuditeeThread():
    global auditee_nick
    global google_modulus
    global google_exponent

    with open(os.path.join(current_sessiondir, 'mypubkey'), 'r') as f: my_pubkey_pem =f.read()
    myPublicKey = rsa.PublicKey.load_pkcs1(my_pubkey_pem)
    myModulus = bigint_to_bytearray(myPublicKey.n)[:10]
    bIsAuditeeRegistered = False
    IRCsocket.settimeout(1)
    while not (bIsAuditeeRegistered or bTerminateAllThreads):
        buffer = ''
        try: buffer = IRCsocket.recv(1024)
        except: continue #1 sec timeout
        if not buffer: continue
        print (buffer)
        messages = buffer.split('\r\n')  #sometimes the IRC server may pack multiple PRIVMSGs into one message separated with /r/n/
        for onemsg in messages:
            msg = onemsg.split()
            if len(msg)==0 : continue  #stray newline
            if msg[0] == "PING":
                IRCsocket.send("PONG %s" % msg[1]) #answer with pong as per RFC 1459
                continue
            if not len(msg) == 4: continue
            if not (msg[1]=='PRIVMSG' and msg[2]==channel_name and msg[3].startswith((':google_pubkey:', ':client_hello:'))): continue
            if msg[3].startswith(':google_pubkey:') and auditee_nick != '': #we already got the first client_hello part
                try:
                    b64_google_pubkey = msg[3][len(':client_hello:'):]
                    google_pubkey =  base64.b64decode(b64_google_pubkey)
                    google_modulus_byte = google_pubkey[:256]
                    google_exponent_byte = google_pubkey[256:]
                    google_modulus = int(google_modulus_byte.encode('hex'),16)
                    google_exponent = int(google_exponent_byte.encode('hex'),16)
                    print ('Auditee successfully verified')
                    bIsAuditeeRegistered = True
                    break
                except:
                    print ('Error while processing google pubkey')
                    auditee_nick=''#erase the nick so that the auditee could try registering again
                    continue
            b64_hello = msg[3][len(':client_hello:'):]
            try:
                hello = base64.b64decode(b64_hello)
                modulus = hello[:10] #this is the first 10 bytes of modulus of auditor's pubkey
                sig = hello[10:] #this is a sig for 'client_hello'. The auditor is expected to have received auditee's pubkey via other channels
                if modulus != myModulus : continue
                rsa.verify('client_hello', sig, auditeePublicKey)
                #we get here if there was no exception
                #msg[0] looks like (without quotes) ":supernick!some_other_info"
                exclamaitionMarkPosition = msg[0].find('!')
                auditee_nick = msg[0][1:exclamaitionMarkPosition]
            except:
                print ('Verification of a hello message failed')
                continue
    if not bIsAuditeeRegistered:
        return ('failure',)
    #else send back a hello message
    signed_hello = rsa.sign('server_hello', myPrivateKey, 'SHA-1')
    b64_signed_hello = base64.b64encode(signed_hello)
    IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditee_nick + ' server_hello:'+b64_signed_hello + ' \r\n')
    time.sleep(2) #send twice because it was observed that the msg would not appear on the chan
    IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditee_nick + ' server_hello:'+b64_signed_hello + ' \r\n')
    
    progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': Auditee has been authorized. Awaiting data...')
    
    thread = threading.Thread(target= receivingThread)
    thread.daemon = True
    thread.start()
            
    thread = threading.Thread(target= process_messages)
    thread.daemon = True
    thread.start()
    
    
      
def start_irc():
    global my_nick
    global IRCsocket
    progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) +': Connecting to irc.freenode.org and joining #tlsnotary')
    
    my_nick= 'user' + ''.join(random.choice('0123456789') for x in range(10))    
    IRCsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    IRCsocket.connect(('chat.freenode.net', 6667))
    IRCsocket.send("USER %s %s %s %s" % ('one1', 'two2', 'three3', 'four4') + '\r\n')
    IRCsocket.send("NICK " + my_nick + '\r\n')  
    IRCsocket.send("JOIN %s" % channel_name + '\r\n')
    progressQueue.put(time.strftime('%H:%M:%S', time.localtime()) + ': Connected to IRC successfully. Waiting for the auditee to join the channel...')

    thread = threading.Thread(target= registerAuditeeThread)
    thread.daemon = True
    thread.start()        
    return 'success'
    

#use miniHTTP server to receive commands from Firefox addon and respond to them
def minihttp_thread(parentthread):    
    #allow three attempts to start mini httpd in case if the port is in use
    bWasStarted = False
    for i in range(3):
        FF_to_backend_port = random.randint(1025,65535)
        print ('Starting mini http server to communicate with Firefox plugin')
        #for the GET request, serve files only from within the datadir
        os.chdir(datadir)
        try:
            httpd = StoppableThreadedHttpServer(('127.0.0.1', FF_to_backend_port), Handler)
            bWasStarted = True
            break
        except Exception, e:
            print ('Error starting mini http server. Maybe the port is in use?', e,end='\r\n')
            continue
    if bWasStarted == False:
        #retval is a var that belongs to our parent class which is ThreadWithRetval
        parentthread.retval = ('failure',)
        return
    #elif minihttpd started successfully
    #Let the invoking thread know that we started successfully the calling process can check thread.retval
    parentthread.retval = ('success', FF_to_backend_port)
    sa = httpd.socket.getsockname()
    print ("Serving HTTP on", sa[0], "port", sa[1], "...",end='\r\n')
    httpd.serve_forever()
    return
  
#a thread which returns a value. This is achieved by passing self as the first argument to a called function
#the calling function can then set parentthread.retval
class ThreadWithRetval(threading.Thread):
    def __init__(self, target, args=()):
        super(ThreadWithRetval, self).__init__(target=target, args = (self,)+args )
    retval = ''


if __name__ == "__main__": 
    #On first run, unpack rsa and pyasn1 archives, check hashes
    rsa_dir = os.path.join(datadir, 'python', 'rsa-3.1.4')
    if not os.path.exists(rsa_dir):
        print ('Extracting rsa-3.1.4.tar.gz')
        with open(os.path.join(datadir, 'python', 'rsa-3.1.4.tar.gz'), 'rb') as f: tarfile_data = f.read()
        #for md5 hash, see https://pypi.python.org/pypi/rsa/3.1.4
        if hashlib.md5(tarfile_data).hexdigest() != 'b6b1c80e1931d4eba8538fd5d4de1355':
            print ('Wrong hash')
            exit(WRONG_HASH)
        os.chdir(os.path.join(datadir, 'python'))
        tar = tarfile.open(os.path.join(datadir, 'python', 'rsa-3.1.4.tar.gz'), 'r:gz')
        tar.extractall()
    #both on first and subsequent runs
    sys.path.append(os.path.join(datadir, 'python', 'rsa-3.1.4'))
    import rsa 
    #init global vars
    myPrivateKey = rsa.key.PrivateKey
    auditeePublicKey = rsa.key.PublicKey
    
    pyasn1_dir = os.path.join(datadir, 'python', 'pyasn1-0.1.7')
    if not os.path.exists(pyasn1_dir):
        print ('Extracting pyasn1-0.1.7.tar.gz')
        with open(os.path.join(datadir, 'python', 'pyasn1-0.1.7.tar.gz'), 'rb') as f: tarfile_data = f.read()
        #for md5 hash, see https://pypi.python.org/pypi/pyasn1/0.1.7
        if hashlib.md5(tarfile_data).hexdigest() != '2cbd80fcd4c7b1c82180d3d76fee18c8':
            print ('Wrong hash')
            exit(WRONG_HASH)
        os.chdir(os.path.join(datadir, 'python'))
        tar = tarfile.open(os.path.join(datadir, 'python', 'pyasn1-0.1.7.tar.gz'), 'r:gz')
        tar.extractall()
    #both on first and subsequent runs
    sys.path.append(os.path.join(datadir, 'python', 'pyasn1-0.1.7'))
    import pyasn1
    from pyasn1.type import univ
    from pyasn1.codec.der import encoder, decoder    
    
    thread = ThreadWithRetval(target= minihttp_thread)
    thread.daemon = True
    thread.start()
    #wait for minihttpd thread to indicate its status
    
    bWasStarted = False
    for i in range(10):
        if thread.retval == '':
            time.sleep(1)
            continue
        elif thread.retval[0] == 'failure':
            print ('Failed to start minihttpd server. Please investigate')
            exit(MINIHTTPD_FAILURE)
        elif thread.retval[0] == 'success':
            bWasStarted = True
            break
        else:
            print ('Unexpected minihttpd server response. Please investigate')
            exit(MINIHTTPD_WRONG_RESPONSE)
    if bWasStarted == False:
        print ('minihttpd failed to start in 10 secs. Please investigate')
        exit(MINIHTTPD_START_TIMEOUT)
    FF_to_backend_port = thread.retval[1]
    
    if OS=='mswin':
        if os.path.isfile(os.path.join(os.getenv('programfiles'), "Mozilla Firefox",  "firefox.exe" )): 
            browser_exepath = os.path.join(os.getenv('programfiles'), "Mozilla Firefox",  "firefox.exe" )
        elif  os.path.isfile(os.path.join(os.getenv('programfiles(x86)'), "Mozilla Firefox",  "firefox.exe" )): 
            browser_exepath = os.path.join(os.getenv('programfiles(x86)'), "Mozilla Firefox",  "firefox.exe" )
        else:
            print ('Please make sure firefox is installed and in your Program Files location', end='\r\n')
            exit(BROWSER_NOT_FOUND)
            
    try:
        ff_proc = subprocess.Popen([browser_exepath, os.path.join('http://127.0.0.1:' + str(FF_to_backend_port) + '/auditor.html')])
    except Exception,e:
        print ("Error starting browser")
        exit(BROWSER_START_ERROR)
    
    #minihttpd server was started successfully, create a unique session dir
    #create a session dir
    time_str = time.strftime("%d-%b-%Y-%H-%M-%S", time.gmtime())
    current_sessiondir = os.path.join(sessionsdir, time_str)
    os.makedirs(current_sessiondir)
    sslkeylogfile = open(os.path.join(current_sessiondir, 'sslkeylogfile'), 'w')
       
    try:
        while True:
            time.sleep(.1)
    except KeyboardInterrupt:
        print ('Interrupted by user')
        bTerminateAllThreads = True