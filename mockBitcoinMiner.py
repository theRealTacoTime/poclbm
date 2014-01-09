import threading
import time
import random
import sys
import Queue
import datetime
import struct

OUTPUT_SIZE = 0x100
TIME_FORMAT = '%d/%m/%Y %H:%M:%S'

def if_else(condition, trueVal, falseVal):
    if condition:
        return trueVal
    else:
        return falseVal

class MockBitcoinMiner(threading.Thread):
    """Mock version of class BitcoinMiner.

    Can be used to test the GUI without actually consuming any resources
    or requiring PyOpenCL to be installed.
    """
    def __init__(self, *args, **kwargs):
        threading.Thread.__init__(self)
        self.workQueue = Queue.Queue()
        self.verbose = True

    def say(self, format, args=()):
        print '%s,' % datetime.datetime.now().strftime(TIME_FORMAT), format % args
        sys.stdout.flush()

    def sayLine(self, format, args=()):
        format = '%s, %s\n' % (datetime.datetime.now().strftime(TIME_FORMAT), format)
        self.say(format, args)                  

    def hashrate(self, rate):
        self.say('%s khash/s', rate)

    def blockFound(self, hash, accepted):
        if random.randint(0,1):
                self.sayLine('%s, %s', (hash, if_else(accepted, 'accepted', 'invalid or stale')))
        else:
                self.sayLine('checking %d' % random.randint(10000,100000))

    def mine(self):
        self.start()
        try:
            while True:
                time.sleep(random.randint(3, 5))
                hash = random.randint(0, 0xffffffff)
                accepted = (random.random() < 0.9)
                self.blockFound(struct.pack('I', long(hash)).encode('hex'), accepted)
        except KeyboardInterrupt:
            self.workQueue.put('stop')
            time.sleep(1.1)
                                                                                                            
    def run(self):
        """Report the hashrate every second with a plausible value."""
        while True:
            if not self.workQueue.empty():
                try:
                    work = self.workQueue.get(True, 1)
                except Queue.Empty:
                    continue
                else:
                    if work == 'stop':
                        return
            time.sleep(1)
            self.hashrate(random.randint(150000, 170000))
                    
if __name__ == "__main__":
    miner = MockBitcoinMiner()
    miner.mine()
    

