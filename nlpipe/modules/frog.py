"""
Wrapper to call the frog server and parse the results as NAF

Assumes that a frog server is listening on FROG_HOST, defaulting to localhost:9887

With 'la machine', this can be done with the following command:
sudo docker run -dp 9887:9887 proycon/lamachine frog -S 9887 --skip=pm

Note that on some machines you need to add --net=host to get port forwarding to work,
possibly related to https://github.com/docker/docker/issues/13914

See: http://languagemachines.github.io/frog/
"""
import csv
import os
from io import StringIO
import socket

from pynlpl.clients.frogclient import FrogClient
from nlpipe.module import Module


class FrogLemmatizer(Module):
    name = "frog"
    
    def __init__(self, server=None):
        if server is None:
            server = os.getenv('FROG_HOST', 'localhost:9887')
        self.host, self.port = server.split(":")

    def check_status(self):
        frogclient = FrogClient(self.host, self.port, returnall=True)
        
    def call_frog(self, text):
        """
        Call frog on the text and return (sent, offset, word, lemma, pos, morphofeat) tuples
        """
        frogclient = FrogClient(self.host, self.port, returnall=True)
        sent = 1
        offset = 0
        for word, lemma, morph, morphofeat, ner, chunk, _p1, _p2 in frogclient.process(text):
            if word is None:
                sent += 1
            else:
                yield (sent, offset, word, lemma, morphofeat, ner, chunk)
                offset += len(word)

    def process(self, text):
        s = StringIO()
        w = csv.writer(s)
        w.writerow(["sentence", "offset", "word", "lemma", "morphofeat", "ner", "chunk"])
        for line in self.call_frog(text):
            w.writerow(list(line))
        return s.getvalue()

    def convert(self, id, result, format):
        assert format in ["csv"]
        # add id to result
        r = csv.reader(StringIO(result), delimiter=',')

        out = StringIO()
        w = csv.writer(out)

        header = next(r)
        w.writerow(["id"] + header)

        for row in r:
            w.writerow([id] + row)

        return out.getvalue()


FrogLemmatizer.register()
