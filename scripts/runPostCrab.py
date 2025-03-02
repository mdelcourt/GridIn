#! /usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division

import argparse
import os
import sys
import subprocess
import tarfile
import contextlib
from pwd import getpwuid

# import SAMADhi stuff
CMSSW_BASE = os.environ['CMSSW_BASE']
SCRAM_ARCH = os.environ['SCRAM_ARCH']
sys.path.append(os.path.join(CMSSW_BASE,'bin', SCRAM_ARCH))

# Add default ingrid storm package
sys.path.append('/nfs/soft/python/python-2.7.5-sl6_amd64_gcc44/lib/python2.7/site-packages/storm-0.20-py2.7-linux-x86_64.egg')
sys.path.append('/nfs/soft/python/python-2.7.5-sl6_amd64_gcc44/lib/python2.7/site-packages/MySQL_python-1.2.3-py2.7-linux-x86_64.egg')

from SAMADhi import Dataset, Sample, File, DbStore
import das_import
from userPrompt import confirm
# import CRAB3 stuff
from CRABAPI.RawCommand import crabCommand

# import a bit of ROOT
import ROOT
ROOT.gROOT.Reset()

def get_file_data(pfn):
    """
    Return the sum of event weights and the entries of the framework output
    """
    f = ROOT.TFile.Open(pfn)
    if not f:
        return (None, None)

    sumw = f.Get("event_weight_sum")
    if sumw:
        sumw = sumw.GetVal()
    else:
        sumw = None

    tree = f.Get("t")
    if tree:
        return (sumw, tree.GetEntriesFast())
    else:
        return (sumw, None)

def get_options():
    """
    Parse and return the arguments provided by the user.
    """
    parser = argparse.ArgumentParser(description='Gather the information on a processed sample and insert the information in SAMADhi')
    parser.add_argument('CrabConfig', type=str, metavar='FILE',
                        help='CRAB3 configuration file (including .py extension).')
    parser.add_argument('--debug', action='store_true', help='More verbose output', dest='debug')
    options = parser.parse_args()
    return options

def load_file(filename):
    directory, module_name = os.path.split(filename)
    module_name = os.path.splitext(module_name)[0]
    path = list(sys.path)
    sys.path.insert(0, directory)
    try:
        module = __import__(module_name)
    finally:
        sys.path[:] = path # restore
    return module

def get_dataset(inputDataset):
    dbstore = DbStore()
    resultset = dbstore.find(Dataset, Dataset.name==inputDataset)
    return list(resultset.values(Dataset.name, Dataset.dataset_id, Dataset.nevents))

def getGitTagRepoUrl(gitCallPath):
    # get the stuff needed to write a valid url: name on github, name of repo, for both origin and upstream
    proc = subprocess.Popen(['git', 'remote', 'show', 'origin'], cwd = gitCallPath, stdout=subprocess.PIPE)
    remoteOrigin = proc.stdout.read()
    remoteOrigin = [x.split(':')[-1].split('/') for x in remoteOrigin.split('\n') if 'Fetch URL' in x]
    remoteOrigin, repoOrigin = remoteOrigin[0]
    repoOrigin = repoOrigin.strip('.git')
    proc = subprocess.Popen(['git', 'remote', 'show', 'upstream'], cwd = gitCallPath, stdout=subprocess.PIPE)
    remoteUpstream = proc.stdout.read()
    remoteUpstream = [x.split(':')[-1].split('/') for x in remoteUpstream.split('\n') if 'Fetch URL' in x]
    remoteUpstream, repoUpstream = remoteUpstream[0]
    repoUpstream = repoUpstream.strip('.git')
    # get the hash of the commit
    # Well, note that actually it should be the tag if a tag exist, the hash is the fallback solution
    proc = subprocess.Popen(['git', 'describe', '--tags', '--always', '--dirty'], cwd = gitCallPath, stdout=subprocess.PIPE)
    gitHash = proc.stdout.read().strip('\n')
    if( 'dirty' in gitHash ):
        raise AssertionError("Aborting: your working tree for repository", repoOrigin, "is dirty, please clean the changes not staged/committed before inserting this in the database") 
    # get the list of branches in which you can find the hash
    proc = subprocess.Popen(['git', 'branch', '-r', '--contains', gitHash], cwd = gitCallPath, stdout=subprocess.PIPE)
    branch = proc.stdout.read()
    if( 'upstream' in branch ):
        url = "https://github.com/" + remoteUpstream + "/" + repoUpstream + "/tree/" + gitHash
        repo = repoUpstream
    elif( 'origin' in branch ):
        url = "https://github.com/" + remoteOrigin + "/" + repoOrigin + "/tree/" + gitHash
        repo = repoOrigin
    else:
        print "PLEASE PUSH YOUR CODE!!! this result CANNOT be reproduced / bookkept outside of your ingrid session, so there is no point into putting it in the database, ABORTING now"
        raise AssertionError("Code from repository " + repoUpstream + " has not been pushed")
    return gitHash, repo, url

def add_sample(NAME, localpath, type, nevents, nselected, AnaUrl, FWUrl, dataset_id, sumw, has_job_processed_everything, dataset_nevents, files, processed_lumi=None):
    dbstore = DbStore()

    sample = None

    # check that source dataset exist
    if dbstore.find(Dataset, Dataset.dataset_id == dataset_id).is_empty():
        raise IndexError("No dataset with such index: %d" % sample.dataset_id)

    # check that there is no existing entry
    update = False
    checkExisting = dbstore.find(Sample, Sample.name == unicode(NAME))
    if checkExisting.is_empty():
        sample = Sample(unicode(NAME), unicode(localpath), unicode(type), nevents)
    else:
        update = True
        sample = checkExisting.one()
        sample.removeFiles(dbstore)

    sample.nevents_processed = nevents
    sample.nevents = nselected
    sample.normalization = 1
    sample.event_weight_sum = sumw
#    sample.luminosity  = 40028954.499 / 1e6 # FIXME: figure out the fix for data whenever the tools will stabilize and be on cvmfs
    sample.code_version = unicode(AnaUrl + ' ' + FWUrl) #NB: limited to 255 characters, but so far so good
    if not has_job_processed_everything:
        sample.user_comment = unicode("Sample was not fully processed, only " + str(nevents) + "/" + str(dataset_nevents) + " events were processed")
    else:
        sample.user_comment = u""
    sample.source_dataset_id = dataset_id
    sample.author = unicode(getpwuid(os.stat(os.getcwd()).st_uid).pw_name)

    if processed_lumi:
        # Convert to json
        import json
        processed_lumi = json.dumps(processed_lumi, separators=(',', ':'))
        sample.processed_lumi = unicode(processed_lumi)
    else:
        sample.processed_lumi = None

    for f in files:
        sample.files.add(f)

    if not update:
        dbstore.add(sample)
        if sample.luminosity is None:
            sample.luminosity = sample.getLuminosity()

        print sample

        if confirm(prompt="Insert into the database?", resp=True):
            dbstore.commit()
            return

    else:
        sample.luminosity = sample.getLuminosity()
        prompt  = "A sample with the same name already exists in the database. Replace by:\n"
        prompt += str(sample)
        prompt += "\n?"
        if confirm(prompt, resp=False):
            dbstore.commit()
            return

    # rollback
    dbstore.rollback()


def main():
    options = get_options()

    import platform
    if 'ingrid' in platform.node():
        storagePrefix = "/storage/data/cms"
    else:
        storagePrefix = "root://cms-xrd-global.cern.ch/"

    print "##### Get information out of the crab config file (work area, dataset, pset)"
    module = load_file(options.CrabConfig)
    workArea = module.config.General.workArea
    requestName = module.config.General.requestName
    psetName = module.config.JobType.psetName
    inputDataset = unicode(module.config.Data.inputDataset)
    print "done"

    print("")
    
    print "##### Check if the dataset exists in the database"
    # if yes then grab its ID
    # if not then run das_import.py to add it
    # print inputDataset
    values = get_dataset(inputDataset)
    # print values
    if( len(values) == 0 ):
        tmp_sysargv = sys.argv
        sys.argv = ["das_import.py", inputDataset]
        print "calling das_import"
        das_import.main()
        print "done"
        sys.argv = tmp_sysargv
        values = get_dataset(inputDataset)
    # if there is more than one sample then we're in trouble, crash here
    assert( len(values) == 1 )
    dataset_name, dataset_id, dataset_nevents = values[0]
    print "done"

    print("")
    
    print "##### Get info from crab (outputs, report)"
    # Since the API outputs AND prints the same data, hide whatever is printed on screen
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    if not options.debug:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    taskdir = os.path.join(workArea, 'crab_' + requestName)
    # list output
    output_files = crabCommand('getoutput', '--dump', dir = taskdir )
    # get crab report
    report = crabCommand('report', dir = taskdir )
    # restore print to stdout 
    if not options.debug:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
#    print "log_files=", log_files
#    print "output_files=", output_files
#    print "report=", report
    print "done"

    print("")

    print "##### Get information from the output files"
    files = []
    for (i, lfn) in enumerate(output_files['lfn']):
        pfn = output_files['pfn'][i]
        files.append({'lfn': lfn, 'pfn': pfn})

    folder = os.path.dirname(output_files['lfn'][0])
    folder = storagePrefix + folder

    db_files = []
    dataset_sumw = 0
    dataset_nselected = 0
    for f in files:
        (sumw, entries) = get_file_data(storagePrefix + f['lfn'])
        if not sumw:
            print("Warning: failed to retrieve sum of event weight for %r" % f['lfn'])

        dataset_sumw += sumw

        if not entries:
            print("Warning: failed to retrieve number of entries for %r" % f['lfn'])

        dataset_nselected += entries

        db_files.append(File(unicode(f['lfn']), unicode(f['pfn']), sumw, entries))

    print "∑w = %.4f" % dataset_sumw
    print "Number of selected events: %d" % dataset_nselected

    print("")

    print "##### Check if the job processed the whole sample"
    has_job_processed_everything = (dataset_nevents == report['eventsRead'])
    is_data = (module.config.Data.splitting == 'LumiBased')
    if has_job_processed_everything:
        print "done"
    else:
        if is_data:
            # This is data, it is expected to not run on everything given we use a lumiMask
            print "done"
        else:
            # Warn
            print "Warning: You are about to add in the DB a sample which has not been completely processed (%d events out of %d, %.2f%%)" % (report['eventsRead'], dataset_nevents, report['eventsRead'] / dataset_nevents * 100)
            print "If you want to update this sample later on with more statistics, simply re-execute this script with the same arguments."

    print("")

    processed_lumi = None
    if is_data:
        processed_lumi = report['analyzedLumis']

    print "##### Figure out the code(s) version"
    # first the version of the framework
    FWHash, FWRepo, FWUrl = getGitTagRepoUrl( os.path.join(CMSSW_BASE, 'src/cp3_llbb/Framework') )
    print "FWUrl=", FWUrl
    # then the version of the analyzer
    AnaHash, AnaRepo, AnaUrl = getGitTagRepoUrl( os.path.dirname( psetName ) )
    print "AnaUrl=", AnaUrl

    print("")

    print "##### Put it all together: write this sample into the database"
    # all the info we have gathered is:
    # workArea
    # requestName
    # psetName
    # inputDataset
    # dataset_id
    # report['eventsRead']) (not necessarily equal to dataset_nevents)
    # log_files
    # output_files
    # report
    # FWHash, FWRepo, FWUrl
    # AnaHash, AnaRepo, AnaUrl
    # dataset_nselected
    # localpath
    NAME = requestName + '_' + FWHash + '_' + AnaRepo + '_' + AnaHash
    add_sample(NAME, folder, "NTUPLES", report['eventsRead'], dataset_nselected, AnaUrl, FWUrl, dataset_id, dataset_sumw, has_job_processed_everything, dataset_nevents, db_files, processed_lumi)

if __name__ == '__main__':
    main() 
