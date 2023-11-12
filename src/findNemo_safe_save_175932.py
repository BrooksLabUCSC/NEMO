from bamUtil import getAlignedReads
from nanoUtil import parseEventAlign, parseSigAlign
from nntUtil import runNNT
from resnet1d import ResNet1D
import argparse
import torch
import numpy as np
import multiprocessing


parser = argparse.ArgumentParser()
# class input
parser.add_argument('--region', '-r', default='', type=str, action='store', help='genomic coordinates to perform modification predictions. E.g. chrI:2000-5000 or chrI.')
parser.add_argument('--bam', '-b', default='', type=str, action='store', help='sorted, indexed, and binarized alignment file.')
parser.add_argument('--genome', '-g', default='', type=str, action='store', help='reference genome fasta file')
parser.add_argument('--eventalign', '-e', default='', type=str, action='store', help='nanopolish eventalign file.')
parser.add_argument('--sigalign', '-s', default='', type=str, action='store', help='sigalign file if sigAlign file already exist. If not, must provide eventalign to generate sigAlign file.')

# class output
parser.add_argument('--outpath', '-o', default='./', type=str, action='store', help='path to store the output files.')
parser.add_argument('--prefix', '-p', default='', type=str, action='store', help='prefix of output file names.')

# modPredict input
parser.add_argument('--model', '-m', default='resnet1d', type=str, action='store', help='deep neural network meodel used for prediction.')
parser.add_argument('--weight', '-w', default='/private/groups/brookslab/gabai/tools/seqUtils/src/nanopore_classification/best_models/addseq_resnet1d.pt', type=str, action='store', help='path to model weight.')
parser.add_argument('--threads', '-t', default=1, type=int, action='store', help='number of threads.')
parser.add_argument('--step', '-step', default=40, type=int, action='store', help='step to bin the region.')
parser.add_argument('--kmerWindow', '-kw', default=75, type=int, action='store', help='kmer window size to extend bin.')
parser.add_argument('--signalWindow', '-sw', default=400, type=int, action='store', help='signal Window size to feed into the model.')
parser.add_argument('--load', '-l', default=1000, type=int, action='store', help='number of reads to load into each iterations. Each iteration will output a file.')


args = parser.parse_args()

class findNemo:

    '''
    class findNemo: predict small molecule modifications from nanopore long-read sequencing data.
    '''
    
    def __init__(self, region, bam, genome, outpath, prefix, eventalign, sigalign):
        
        '''
        self:
            self.alignment: dict, stores reference an reads mapped to region.
            self.chrom: str, chromosome name.
            self.qStart: int, query start position.
            self.qEnd: int, query end position
            self.outpath: str, path to store the output files.
        Input:
            region: genomic coordinates to perform modification predictions. Format: 'chrI:2000-5000' or 'chrI'.
            bam: sorted, indexed, and binarized alignment file.
            genome: reference genome fasta file.
        Output:
            outpath: path to store the output files.
            prefix: prefix of output file names.
            eventalign: nanopolish eventalign file.
            sigalign: sigalign file if sigAlign file already exist. If not, must provide eventalign to generate sigAlign file.
        '''

        # Fetch reads aligned to the region
        self.alignment, self.chrom, self.qStart, self.qEnd = getAlignedReads(bam, region = region, genome = genome)
        self.outpath = outpath
        self.prefix = prefix

        # Index reads to avoid storing the long readnames. 
        self.reads = {r:i for r,i in zip(list(self.alignment.keys())[1:], range(len(self.alignment)-1))}
        # Store the id index match into a file.
        readFh = open(outpath + prefix + region + '_readID.tsv', 'w')
        for k,v in self.reads.items(): readFh.write('{read}\t{index}\n'.format(k,v))
        readFh.close()
        print(len(self.reads), " reads mapped to ", region)

        if sigalign:
            self.sigalign = sigalign
        elif eventalign:
            self.sigalign = outpath + prefix + str(region) + '_sig.tsv'
            parseEventAlign(eventAlign = eventalign, reads = self.reads, outfile = self.sigalign)
        else:
            print('Error: None of sigalign or eventalign file is provided!')

        self.gene_regions = {
            'PHO5': 'chrII:429000-435000',
            'CLN2': 'chrXVI:66000-67550',
            'HMR': 'chrIII:290000-299000',
            'AUA1': 'chrVI:114000-116000',
            'EMW1': 'chrXIV:45000-50000',
            'NRG2': 'chrII:370000-379000',
            'RDN37': 'chrXII:450300-459300'
            }

    def doWork(work):
    
        (readID, bins, step, aStart, aEnd, sigList, sigLenList, kmerWindow, signalWindow, device, model, weight) = work
        
        scores = runNNT(readID, bins, step, aStart, aEnd, sigList, sigLenList, kmerWindow, signalWindow, device, model, weight)
        
        return scores
    
    def modPredict(model, weight, threads, step, kmerWindow, signalWindow, load):
        
        torch.multiprocessing.set_start_method('spawn')
        
        bins = np.arange(self.qStart, self.End, kmerWindow)
        
        device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        
        # only one model available
        models = {
            'resnet1D': ResNet1D(
                in_channels=1,
                base_filters=128,
                kernel_size=3,
                stride=2,
                groups=1,
                n_block=8,
                n_classes=2,
                downsample_gap=2,
                increasefilter_gap=4,
                use_do=False)
            }
        
        print('Device type: ', device)
        
        # Use the specified threads number or maximum available CPU cores
        num_processes = min(threads, multiprocessing.cpu_count())
        pool = multiprocessing.Pool(processes=num_processes)

        # Total work to be done are stored in a list
        works = [(readID, bins, step, aStart, aEnd, sigList, sigLenList, kmerWindow, signalWindow, device, models[model], weight) 
                  for readID, aStart, aEnd, strand, sigList, sigLenList in parseSigAlign(self.sigalign, self.alignment)]
        
        for x in range(0, len(works), load):
            # split total work by load
            works_per_load = works[x:x+load]
            
            predOut = self.outpath + self.prefix + str(self.region) + '_' + str(x) + '_prediction.tsv'

            # Use the pool.map() function to process reads in parallel
            outs = pool.map(self.outpath , works_per_load)

            # Close the pool to release resources
            pool.close()
            pool.join()

            # Write the results from current work load
            predOutFh = open(predOut, 'w')
            for r in range(len(outs)):
                out = outs[r]
                readID = works_per_load[r][0]
                bin_start = next(iter(out))
                predOutFh.write('{readID}\t{bin_start}\t{scores}'.format(readID = readID, bin_start = bin_start, scores = ','.join(map(str, out.values()))))
            predOutFh.close()


if __name__ == '__main__':
    myprediction = findNemo(args.region, args.bam, args.genome, args.outpath, args.prefix, args.eventalign, args.sigalign)
    myprediction.modPredict(args.model, args.weight, args.threads, args.step, args.kmerWindow, args.signalWindow, args.load)