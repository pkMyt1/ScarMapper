"""

@author: Dennis A. Simpson
         University of North Carolina at Chapel Hill
         Chapel Hill, NC  27599
@copyright: 2020
"""
import collections
import datetime
import itertools
import os
import subprocess
import time
import pathos
import pysam
from natsort import natsort
from Valkyries import Tool_Box, Sequence_Magic, FASTQ_Tools
from scarmapper import SlidingWindow

__author__ = 'Dennis A. Simpson'
__version__ = '0.17.0'
__package__ = 'ScarMapper'


class ScarSearch:
    def __init__(self, log, args, version, run_start, target_dict, index_dict, index_name, sequence_list):
        self.log = log
        self.args = args
        self.version = version
        self.run_start = run_start
        self.target_dict = target_dict
        self.index_dict = index_dict
        self.index_name = index_name
        self.sequence_list = sequence_list
        self.summary_data = None
        self.target_region = ""
        self.cutsite = None
        self.lower_limit = None
        self.upper_limit = None
        self.target_length = None
        self.left_target_windows = []
        self.right_target_windows = []

        if self.target_dict[index_dict[index_name][7]][5] == "YES":
            self.hr_donor = Sequence_Magic.rcomp(args.HR_Donor)
        else:
            self.hr_donor = args.HR_Donor

        self.data_processing()

    def window_mapping(self):
        """
        Predetermine all the sliding window results for the target region.
        """

        self.target_length = len(self.target_region)

        # Set upper and lower limit to be 5 nt from end of primers
        self.lower_limit = 15
        self.upper_limit = self.target_length-15
        lft_position = self.cutsite-10
        rt_position = self.cutsite

        while rt_position > self.lower_limit:
            self.left_target_windows.append(self.target_region[lft_position:rt_position])
            lft_position -= 1
            rt_position -= 1

        lft_position = self.cutsite
        rt_position = self.cutsite+10
        while lft_position < self.upper_limit:
            self.right_target_windows.append(self.target_region[lft_position:rt_position])
            lft_position += 1
            rt_position += 1

    def trim_phasing(self, seq, left_read):
        """
        Trim and anchor the 5' end of each read to the appropriate end of target region
        :param seq:
        :param left_read:
        :return:
        """

        right_position = 5
        right_limit = 25
        lft_position = 0
        if left_read:
            target_block = self.target_region[:5]
        else:
            target_block = Sequence_Magic.rcomp(self.target_region)[:5]
        block_found = False

        while right_position < right_limit and not block_found:
            left_block = seq[lft_position:right_position]
            if left_block == target_block:
                seq = seq[lft_position:]
                block_found = True
            lft_position += 1
            right_position += 1

        return block_found, seq

    @staticmethod
    def simple_consensus(left_seq, right_seq):
        """
        Generate a simple consensus sequence from trimmed read 1 and read 2 sequences.
        :param left_seq:
        :param right_seq:
        :return:
        """
        rt_seq = Sequence_Magic.rcomp(right_seq)
        right_limit = len(right_seq) - 25
        left_limit = 25
        window_size = 7
        left_position = len(left_seq) - window_size
        right_position = len(left_seq)
        consensus_seq = ""

        while left_position > left_limit and not consensus_seq:
            left_block = left_seq[left_position:right_position]
            lft_position = 0
            rt_position = window_size

            while rt_position < right_limit and not consensus_seq:
                test_window = rt_seq[lft_position:rt_position]

                if left_block == test_window:
                    consensus_seq = left_seq + rt_seq[rt_position:]

                lft_position += 1
                rt_position += 1

            left_position -= 1
            right_position -= 1

        return consensus_seq

    def data_processing(self):
        """
        Generate the consensus sequence and find indels.  Write the frequency file.  Called by pathos pool

        :return:
        """

        self.log.info("Begin Processing {}".format(self.index_name))
        """
        Summary_Data List: index_name, total aberrant, left deletions, right deletions, total deletions, left insertions, 
        right insertions, total insertions, microhomology, number filtered, target_name
        """
        target_name = self.index_dict[self.index_name][7]
        self.summary_data = [self.index_name, 0, 0, 0, 0, 0, [0, 0], [0, 0], 'junction data', target_name, [0, 0]]
        junction_type_data = [0, 0, 0, 0, 0]
        read_results_list = []
        results_freq_dict = collections.defaultdict(list)
        refseq = pysam.FastaFile(self.args.RefSeq)

        try:
            # Get the genomic 5' coordinate of the reference target region.
            start = int(self.target_dict[target_name][2])
        except IndexError:
            self.log.error("Target file incorrectly formatted for {}".format(target_name))
            return

        # Get the genomic 3' coordinate of the reference target region.
        stop = int(self.target_dict[target_name][3])

        chrm = self.target_dict[target_name][1]

        # Get the sequence of the sgRNA.
        sgrna = self.target_dict[target_name][4]
        self.target_region = refseq.fetch(chrm, start, stop)
        self.cutsite_search(target_name, sgrna, chrm, start, stop)
        self.window_mapping()
        loop_count = 0
        start_time = time.time()
        split_time = start_time

        # Extract and process read 1 and read 2 from our list of sequences.
        for seq in self.sequence_list:
            loop_count += 1

            if loop_count % 5000 == 0:
                self.log.info("Processed {} reads of {} for {} in {} seconds. Elapsed time: {} seconds."
                              .format(loop_count, len(self.sequence_list), self.index_name, time.time() - split_time,
                                      time.time() - start_time))
                split_time = time.time()

            if not self.args.PEAR:
                left_seq, right_seq = seq
                # Muscle will not properly gap sequences with an overlap smaller than about 50 nucleotides.
                # consensus_seq = \
                #     self.gapped_aligner(">left\n{}\n>right\n{}\n"
                #                         .format(left_seq, Sequence_Magic.rcomp(right_seq)))

                consensus_seq = self.simple_consensus(left_seq, right_seq)

            else:
                # If we are using pear to generate the consensus.
                consensus_seq = seq

            # Consensus sequence creation failed.
            if not consensus_seq:
                self.summary_data[7][1] += 1
                continue

            # No need to attempt an analysis of bad data.
            if consensus_seq.count("N") / len(consensus_seq) > float(self.args.N_Limit):
                self.summary_data[7][0] += 1
                continue

            # No need to analyze sequences that are too short.
            if len(consensus_seq) <= int(self.args.Minimum_Length):
                self.summary_data[7][0] += 1
                continue

            '''
            The summary_data list contains information for a single library.  [0] index name; [1] reads passing all 
            filters; [2] left junction count; [3] right junction count; [4] insertion count; [5] microhomology count; 
            [6] [No junction count, no cut count]; [7] [consensus N + short filtered count, failed consensus 
            creation count]; [8] junction_type_data list; [9] target name; 10 [HR left junction count, HR right 
            junction count]

            The junction_type_data list contains the repair type category counts.  [0] TsEJ, del_size >= 4 and 
            microhomology_size >= 2; [1] NHEJ, del_size < 4 and ins_size < 5; [2] insertions >= 5 
            [3] Junctions with scars not represented by the other categories; [4] Non-MH Deletions, del_size >= 4 and 
            microhomology_size < 2 and ins_size < 5
            '''
            # count reads that pass the read filters
            self.summary_data[1] += 1

            # The cutwindow is used to filter out false positives.
            cutwindow = self.target_region[self.cutsite-4:self.cutsite+4]

            sub_list, self.summary_data = \
                SlidingWindow.sliding_window(
                    consensus_seq, self.target_region, self.cutsite, self.target_length, self.lower_limit,
                    self.upper_limit, self.summary_data, self.left_target_windows, self.right_target_windows, cutwindow,
                    self.hr_donor)

            '''
            The sub_list holds the data for a single consensus read.  These data are [left deletion, right deletion, 
            insertion, microhomology, consensus sequence].  The list could be empty if nothing was found or the 
            consensus was too short.
            '''

            if sub_list:
                read_results_list.append(sub_list)
                freq_key = "{}|{}|{}|{}|{}".format(sub_list[0], sub_list[1], sub_list[2], sub_list[3], sub_list[9])

            else:
                continue

            if freq_key in results_freq_dict:
                results_freq_dict[freq_key][0] += 1
            else:
                results_freq_dict[freq_key] = [1, sub_list]

        self.log.info("Finished Processing {}".format(self.index_name))

        # Write frequency results file
        self.frequency_output(self.index_name, results_freq_dict, junction_type_data)

        # Format and output raw data if user has so chosen.
        if self.args.OutputRawData:
            self.raw_data_output(self.index_name, read_results_list)

        return self.summary_data

    def common_page_header(self, index_name):
        """
        Generates common page header for frequency and raw data files.
        :param index_name:
        :return:
        """
        date_format = "%a %b %d %H:%M:%S %Y"
        run_stop = datetime.datetime.today().strftime(date_format)
        target_name = self.index_dict[index_name][7]
        sgrna = self.target_dict[target_name][4]
        sample_name = "{}.{}".format(self.index_dict[index_name][5], self.index_dict[index_name][6])

        hr_donor = ""
        if self.args.HR_Donor:
            hr_donor = "# HR Donor: {}\n".format(self.args.HR_Donor)

        page_header = \
            "# ScarMapper Search v{}\n# Run Start: {}\n# Run End: {}\n# Sample Name: {}\n# Locus Name: {}\n" \
            "# sgRNA: {}\n{}\n"\
            .format(self.version, self.run_start, run_stop, sample_name, target_name, sgrna, hr_donor)

        return page_header

    def frequency_output(self, index_name, results_freq_dict, junction_type_data):
        """
        Format data and write frequency file.

        :param index_name:
        :param results_freq_dict:
        :param junction_type_data:
        """
        self.log.info("Writing Frequency File for {}".format(index_name))

        target_name = self.index_dict[index_name][7]

        freq_results_outstring = \
            "{}# Total\tFrequency\tScar Type\tLeft Deletions\tRight Deletions\tDeletion Size\tMicrohomology\t" \
            "Microhomology Size\tInsertion\tInsertion Size\tLeft Template\tRight Template\tConsensus Left Junction\t" \
            "Consensus Right Junction\tTarget Left Junction\tTarget Right Junction\tConsensus\tTarget Region\n"\
            .format(self.common_page_header(index_name))

        for freq_key in results_freq_dict:
            key_count = results_freq_dict[freq_key][0]

            try:
                key_frequency = key_count / (self.summary_data[1] - self.summary_data[6][1])
            except ZeroDivisionError:
                key_frequency = 0

            lft_del = len(results_freq_dict[freq_key][1][0])
            rt_del = len(results_freq_dict[freq_key][1][1])
            insertion = results_freq_dict[freq_key][1][2]
            ins_size = len(insertion)
            consensus = results_freq_dict[freq_key][1][4]
            microhomology = results_freq_dict[freq_key][1][3]
            microhomology_size = len(microhomology)
            del_size = lft_del + rt_del + microhomology_size
            consensus_lft_junction = results_freq_dict[freq_key][1][5]
            consensus_rt_junction = results_freq_dict[freq_key][1][6]
            ref_lft_junction = results_freq_dict[freq_key][1][7]
            ref_rt_junction = results_freq_dict[freq_key][1][8]
            lft_template = ""
            rt_template = ""
            target_sequence = self.target_region
            scar_type = "Other"

            # If sgRNA is from 3' strand we need to swap labels and reverse compliment sequences.
            if self.target_dict[target_name][5] == "YES":
                rt_del = len(results_freq_dict[freq_key][1][0])
                lft_del = len(results_freq_dict[freq_key][1][1])
                microhomology = Sequence_Magic.rcomp(microhomology)
                insertion = Sequence_Magic.rcomp(insertion)
                consensus = Sequence_Magic.rcomp(consensus)
                target_sequence = Sequence_Magic.rcomp(self.target_region)
                tmp_con_lft = consensus_lft_junction
                tmp_target_lft = ref_lft_junction
                consensus_lft_junction = len(consensus)-consensus_rt_junction
                consensus_rt_junction = len(consensus)-tmp_con_lft
                ref_lft_junction = len(self.target_region)-ref_rt_junction
                ref_rt_junction = len(self.target_region)-tmp_target_lft

            # HR counts
            if results_freq_dict[freq_key][1][9] == "HR":
                scar_type = "HR"

            # TMEJ counts
            elif del_size >= 4 and microhomology_size >= 2:
                junction_type_data[0] += key_count
                scar_type = "TsEJ"

            # NHEJ counts
            elif del_size < 4 and ins_size < 5:
                junction_type_data[1] += key_count
                scar_type = "NHEJ"

            # Non-Microhomology Deletions
            elif del_size >= 4 and microhomology_size < 2 and ins_size < 5:
                junction_type_data[4] += key_count
                scar_type = "Non-MH Deletion"

            # Large Insertions with or without Deletions:
            elif ins_size >= 5:
                junction_type_data[2] += key_count
                scar_type = "Insertion"
                lft_template, rt_template = \
                    self.templated_insertion_search(insertion, ref_lft_junction, ref_rt_junction, target_name)

            # Scars not part of the previous four
            else:
                junction_type_data[3] += key_count

            freq_results_outstring += "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n" \
                .format(key_count, key_frequency, scar_type, lft_del, rt_del, del_size, microhomology,
                        microhomology_size, insertion, ins_size, lft_template, rt_template, consensus_lft_junction,
                        consensus_rt_junction, ref_lft_junction, ref_rt_junction, consensus, target_sequence)

        freq_results_file = \
            open("{}{}_{}_ScarMapper_Frequency.txt"
                 .format(self.args.WorkingFolder, self.args.Job_Name, index_name), "w")

        freq_results_file.write(freq_results_outstring)
        freq_results_file.close()

        # add the junction list to the summary data
        self.summary_data[8] = junction_type_data

    def templated_insertion_search(self, insertion, lft_target_junction, rt_target_junction, target_name):
        """
        Search for left and right templates for insertions.
        :param insertion:
        :param lft_target_junction:
        :param rt_target_junction:
        :param target_name:
        :return:
        """
        lft_query1 = Sequence_Magic.rcomp(insertion[:5])
        lft_query2 = insertion[-5:]
        rt_query1 = insertion[:5]
        rt_query2 = Sequence_Magic.rcomp(insertion[-5:])
        lower_limit = lft_target_junction-50
        upper_limit = rt_target_junction+50
        left_not_found = True
        right_not_found = True
        lft_template = ""
        rt_template = ""

        # Set starting positions and search for left template
        lft_position = lft_target_junction-5
        rt_position = lft_target_junction

        while left_not_found and rt_position > lower_limit:
            target_segment = self.target_region[lft_position:rt_position]

            if lft_query1 == target_segment or lft_query2 == target_segment:
                lft_template = target_segment
                if self.target_dict[target_name][5] == "YES":
                    lft_template = Sequence_Magic.rcomp(target_segment)
                left_not_found = False

            lft_position -= 1
            rt_position -= 1

        # Reset starting positions and search for right template
        lft_position = rt_target_junction
        rt_position = rt_target_junction+5
        while right_not_found and lft_position < upper_limit:
            target_segment = self.target_region[lft_position:rt_position]
            if rt_query1 == target_segment or rt_query2 == target_segment:
                rt_template = target_segment
                if self.target_dict[target_name][5] == "YES":
                    rt_template = Sequence_Magic.rcomp(target_segment)
                right_not_found = False

            lft_position += 1
            rt_position += 1

        return lft_template, rt_template

    def raw_data_output(self, index_name, read_results_list):
        """
        Handle formatting and writing raw data.
        :param index_name:
        :param read_results_list:
        """
        results_file = open("{}{}_{}_ScarMapper_Raw_Data.txt"
                            .format(self.args.WorkingFolder, self.args.Job_Name, index_name), "w")

        results_outstring = \
            "{}Left Deletions\tRight Deletions\tDeletion Size\tMicrohomology\tInsertion\tInsertion Size\t" \
            "Consensus Left Junction\tConsensus Right Junction\tRef Left Junction\tRef Right Junction\t" \
            "Consensus\tTarget Region\n".format(self.common_page_header(index_name))

        for data_list in read_results_list:
            lft_del = len(data_list[0])
            rt_del = len(data_list[1])
            microhomology = data_list[3]
            del_size = lft_del + rt_del + len(microhomology)
            total_ins = data_list[2]
            ins_size = len(total_ins)
            consensus = data_list[4]
            target_region = self.target_region
            target_name = self.index_dict[index_name][7]
            consensus_lft_junction = data_list[5]
            consensus_rt_junction = data_list[6]
            ref_lft_junction = data_list[7]
            ref_rt_junction = data_list[8]

            # If sgRNA is from 3' strand we need to swap labels and reverse compliment sequences.
            if self.target_dict[target_name][5] == "YES":
                rt_del = len(data_list[0])
                lft_del = len(data_list[1])
                consensus = Sequence_Magic.rcomp(data_list[4])
                target_region = Sequence_Magic.rcomp(self.target_region)
                microhomology = Sequence_Magic.rcomp(data_list[3])
                total_ins = Sequence_Magic.rcomp(data_list[2])

                tmp_con_lft = consensus_lft_junction
                tmp_target_lft = ref_lft_junction
                consensus_lft_junction = len(consensus)-consensus_rt_junction
                consensus_rt_junction = len(consensus)-tmp_con_lft
                ref_lft_junction = len(self.target_region)-ref_rt_junction
                ref_rt_junction = len(self.target_region)-tmp_target_lft

            # skip unaltered reads.
            if del_size == 0 and ins_size == 0:
                continue

            results_outstring += "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n" \
                .format(lft_del, rt_del, del_size, microhomology, total_ins, ins_size, consensus_lft_junction,
                        consensus_rt_junction, ref_lft_junction, ref_rt_junction, consensus, target_region)

        results_file.write(results_outstring)
        results_file.close()

    def cutsite_search(self, target_name, sgrna, chrm, start, stop):
        """
        Find the sgRNA cutsite on the gapped genomic DNA.
        :param stop:
        :param start:
        :param chrm:
        :param sgrna:
        :param target_name:
        """

        lft_position = 0
        rt_position = len(sgrna)
        upper_limit = len(self.target_region)-1
        working_sgrna = sgrna
        rcomp_sgrna = False

        if self.target_dict[target_name][5] == 'YES':
            working_sgrna = Sequence_Magic.rcomp(sgrna)
            rcomp_sgrna = True

        cutsite_found = False
        while not cutsite_found and rt_position < upper_limit:
            if self.target_region[lft_position:rt_position] == working_sgrna:
                cutsite_found = True

                if rcomp_sgrna:
                    self.cutsite = lft_position+3
                else:
                    self.cutsite = rt_position-3

            lft_position += 1
            rt_position += 1

        if not cutsite_found:
            self.log.error("sgRNA {} does not map to locus {}; chr{}:{}-{}.  Check --TargetFile and try again."
                           .format(sgrna, target_name, chrm, start, stop))
            raise SystemExit(1)
            os._exit(1)

    def gapped_aligner(self, fasta_data):
        """
        Generates and returns a simple consensus from the given FASTA data using Muscle.
        :param: self
        :param: fasta_data
        :return:
        """

        # Create gapped alignment file in FASTA format using MUSCLE
        # cmd = ['muscle', "-quiet", "-maxiters", "1", "-diags"]
        cmd = ['muscle', "-quiet", "-refinewindow", "10"]
        muscle = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)

        output, err = muscle.communicate(input=fasta_data)
        if err:
            self.log.error(err)

        cat_line = ""
        first_line = True
        gapped_alignment_dict = collections.defaultdict(str)
        key = ""
        # Tool_Box.debug_messenger(output)
        list_output = list(output.splitlines())
        consensus_seq = ""

        for line in list_output:
            if ">" in line:
                if not first_line:
                    gapped_alignment_dict[key] = cat_line
                    cat_line = ""
                first_line = False
                key = line.split(">")[1].strip("\n")
            else:
                cat_line += line.strip("\n")

            gapped_alignment_dict[key] = cat_line

        # Build a simple contig from the gapped alignment of the paired reads
        first_lft = False
        for i, (lft, rt) in enumerate(zip(gapped_alignment_dict["left"], gapped_alignment_dict["right"])):

            if not first_lft:
                if lft is not "-" and gapped_alignment_dict["left"][i + 1] is not "-":
                    first_lft = True

            if not first_lft:
                continue

            if lft == rt:
                consensus_seq += lft
            elif lft is "-":
                consensus_seq += rt
            elif rt is "-":
                consensus_seq += lft
            else:
                consensus_seq += "N"

        return consensus_seq


class DataProcessing:
    def __init__(self, log, args, run_start, version, targeting, fq1=None, fq2=None):
        self.log = log
        self.args = args
        self.version = version
        self.date_format = "%a %b %d %H:%M:%S %Y"
        self.run_start = run_start
        self.fastq_outfile_dict = None
        self.target_dict = targeting.targets
        self.phase_dict = targeting.phasing
        self.phase_count = collections.defaultdict(lambda: collections.defaultdict(int))
        self.index_dict = self.dictionary_build()
        self.results_dict = collections.defaultdict(list)
        self.sequence_dict = collections.defaultdict(list)
        self.read_count_dict = collections.defaultdict()
        self.fastq1 = fq1
        self.fastq2 = fq2
        self.read_count = 0

    def consensus_demultiplex(self):
        """
        Takes a FASTQ file of consensus reads and identifies each by index.  Handles writing demultiplexed FASTQ if
        user desired.
        """
        self.log.info("Consensus Index Search")
        # fastq1_short_count = 0
        # fastq2_short_count = 0
        eof = False
        start_time = time.time()
        split_time = time.time()
        fastq_file_name_list = []
        fastq_data_dict = collections.defaultdict(lambda: collections.defaultdict(list))
        while not eof:
            # Debugging Code Block
            if self.args.Verbose == "DEBUG":
                read_limit = 500000
                if self.read_count > read_limit:
                    if self.args.Demultiplex:
                        for index_name in fastq_data_dict:
                            r1_data = fastq_data_dict[index_name]["R1"]
                            r1, r2 = self.fastq_outfile_dict[index_name]
                            r1.write(r1_data)
                            r1.close()

                    Tool_Box.debug_messenger("Limiting Reads Here to {}".format(read_limit))
                    eof = True

            try:
                fastq1_read = next(self.fastq1.seq_read())

            except StopIteration:
                if self.args.Demultiplex:
                    for index_name in fastq_data_dict:
                        r1_data = fastq_data_dict[index_name]["R1"]
                        r1, r2 = self.fastq_outfile_dict[index_name]
                        r1.write(r1_data)
                        r1.close()

                eof = True
                continue

            self.read_count += 1
            if self.read_count % 100000 == 0:
                elapsed_time = int(time.time() - start_time)
                block_time = int(time.time() - split_time)
                split_time = time.time()
                self.log.info("Processed {} reads in {} seconds.  Total elapsed time: {} seconds."
                              .format(self.read_count, block_time, elapsed_time))

            # Match read with library index.
            match_found, left_seq, right_seq, index_name, fastq1_read, fastq2_read = \
                self.index_matching(fastq1_read)

            if match_found:
                locus = self.index_dict[index_name][7]
                phase_key = "{}+{}".format(index_name, locus)
                r2_found = False
                r1_found = False
                if self.args.Platform == "Illumina":

                    # Score the phasing and place the reads in a dictionary.
                    for r2_phase, r1_phase in zip(self.phase_dict[locus]["R2"], self.phase_dict[locus]["R1"]):
                        r2_phase_name = r2_phase[1]
                        r1_phase_name = r1_phase[1]
                        self.phase_count[phase_key]["Phase " + r1_phase_name] += 0
                        self.phase_count[phase_key]["Phase " + r2_phase_name] += 0

                        # The phasing is the last N nucleotides of the consensus.
                        if r2_phase[0] == Sequence_Magic.rcomp(fastq1_read.seq[-len(r2_phase[0]):]) and not r2_found:
                            self.phase_count[phase_key]["Phase "+r2_phase_name] += 1
                            r2_found = True

                        if r1_phase[0] == fastq1_read.seq[:len(r1_phase[0])] and not r1_found:
                            self.phase_count[phase_key]["Phase "+r1_phase_name] += 1
                            r1_found = True
                    # if no phasing is found then note that.
                    if not r2_found:
                        self.phase_count[phase_key]["No Read 2 Phasing"] += 1
                    if not r1_found:
                        self.phase_count[phase_key]["No Read 1 Phasing"] += 1

                    # The adapters on AAVS1.1 are reversed causing the reads to be reversed.
                    if locus == "AAVS1.1":
                        self.sequence_dict[index_name].append(fastq1_read.seq)
                    else:
                        self.sequence_dict[index_name].append(fastq1_read.seq)

                elif self.args.Platform == "Ramsden":
                    self.sequence_dict[index_name].append(Sequence_Magic.rcomp(fastq1_read.seq))
                else:
                    self.log.error("--Platform {} not correctly defined.  Edit parameter file and try again"
                                   .format(self.args.Platform))
                    raise SystemExit(1)

                if self.args.Demultiplex:
                    fastq_data_dict[index_name]["R1"].append([fastq1_read.name, fastq1_read.seq, fastq1_read.qual])

                    fastq_file_name_list.append("{}{}_{}_Consensus.fastq"
                                                .format(self.args.WorkingFolder, self.args.Job_Name, index_name))

            elif self.args.Demultiplex and not match_found:
                fastq_data_dict['unknown']["R1"].append([fastq1_read.name, fastq1_read.seq, fastq1_read.qual])

                fastq_file_name_list.append("{}{}_Unknown_Consensus.fastq"
                                            .format(self.args.WorkingFolder, self.args.Job_Name))

        if self.args.Demultiplex:
            self.fastq_compress(list(set(fastq_file_name_list)))

    def fastq_compress(self, fastq_file_name_list):
        """
        Take a list of file names and gzip each file.
        :param fastq_file_name_list:
        """
        self.log.info("Spawning {} Jobs to Compress {} Files.".format(self.args.Spawn, len(fastq_file_name_list)))

        p = pathos.multiprocessing.Pool(int(self.args.Spawn))
        p.starmap(Tool_Box.compress_files, zip(fastq_file_name_list, itertools.repeat(self.log)))

        self.log.info("All Files Compressed")

    def demultiplex(self):
        """
        Finds reads by index.  Handles writing demultiplexed FASTQ if user desired.
        """
        self.log.info("Index Search")
        eof = False
        start_time = time.time()
        split_time = time.time()
        fastq_file_name_list = []
        fastq_data_dict = collections.defaultdict(lambda: collections.defaultdict(list))
        while not eof:
            # Debugging Code Block
            if self.args.Verbose == "DEBUG":
                read_limit = 500000
                if self.read_count > read_limit:
                    if self.args.Demultiplex:
                        for index_name in fastq_data_dict:
                            r1_data = fastq_data_dict[index_name]["R1"]
                            r2_data = fastq_data_dict[index_name]["R2"]
                            r1, r2 = self.fastq_outfile_dict[index_name]
                            r1.write(r1_data)
                            r2.write(r2_data)
                            r1.close()
                            r2.close()
                    Tool_Box.debug_messenger("Limiting Reads Here to {}".format(read_limit))
                    eof = True

            try:
                fastq1_read = next(self.fastq1.seq_read())
                fastq2_read = next(self.fastq2.seq_read())

            except StopIteration:
                if self.args.Demultiplex:
                    for index_name in fastq_data_dict:
                        r1_data = fastq_data_dict[index_name]["R1"]
                        r2_data = fastq_data_dict[index_name]["R2"]
                        r1, r2 = self.fastq_outfile_dict[index_name]
                        r1.write(r1_data)
                        r2.write(r2_data)
                        r1.close()
                        r2.close()
                eof = True
                continue

            self.read_count += 1
            if self.read_count % 100000 == 0:
                elapsed_time = int(time.time() - start_time)
                block_time = int(time.time() - split_time)
                split_time = time.time()
                self.log.info("Processed {} reads in {} seconds.  Total elapsed time: {} seconds."
                              .format(self.read_count, block_time, elapsed_time))

            # Match read with library index.
            match_found, left_seq, right_seq, index_name, fastq1_read, fastq2_read = \
                self.index_matching(fastq1_read, fastq2_read)

            if match_found:
                locus = self.index_dict[index_name][7]
                phase_key = "{}+{}".format(index_name, locus)
                r2_found = False
                r1_found = False
                if self.args.Platform == "Illumina":
                    # Score the phasing and place the reads in a dictionary.
                    for r2_phase, r1_phase in zip(self.phase_dict[locus]["R2"], self.phase_dict[locus]["R1"]):
                        r2_phase_name = r2_phase[1]
                        r1_phase_name = r1_phase[1]
                        self.phase_count[phase_key]["Phase " + r1_phase_name] += 0
                        self.phase_count[phase_key]["Phase " + r2_phase_name] += 0

                        if r2_phase[0] == left_seq[:len(r2_phase[0])] and not r2_found:
                            self.phase_count[phase_key]["Phase "+r2_phase_name] += 1
                            r2_found = True

                        if r1_phase[0] == right_seq[:len(r1_phase[0])] and not r1_found:
                            self.phase_count[phase_key]["Phase "+r1_phase_name] += 1
                            r1_found = True
                    # if no phasing is found then note that.
                    if not r2_found:
                        self.phase_count[phase_key]["No Read 2 Phasing"] += 1
                    if not r1_found:
                        self.phase_count[phase_key]["No Read 1 Phasing"] += 1

                    # The adapters on AAVS1.1 are reversed causing the reads to be reversed.
                    if locus == "AAVS1.1":
                        self.sequence_dict[index_name].append([left_seq, right_seq])
                    else:
                        self.sequence_dict[index_name].append([right_seq, left_seq])

                elif self.args.Platform == "Ramsden":
                    self.sequence_dict[index_name].append([left_seq, right_seq])
                else:
                    self.log.error("--Platform {} not correctly defined.  Edit parameter file and try again"
                                   .format(self.args.Platform))
                    raise SystemExit(1)

                if self.args.Demultiplex:
                    fastq_data_dict[index_name]["R1"].append([fastq1_read.name, fastq1_read.seq, fastq1_read.qual])
                    fastq_data_dict[index_name]["R2"].append([fastq2_read.name, fastq2_read.seq, fastq2_read.qual])
                    fastq_file_name_list.append("{}{}_{}_R1.fastq"
                                                .format(self.args.WorkingFolder, self.args.Job_Name, index_name))
                    fastq_file_name_list.append("{}{}_{}_R2.fastq"
                                                .format(self.args.WorkingFolder, self.args.Job_Name, index_name))

            elif self.args.Demultiplex and not match_found:
                fastq_data_dict['unknown']["R1"].append([fastq1_read.name, fastq1_read.seq, fastq1_read.qual])
                fastq_data_dict['unknown']["R2"].append([fastq2_read.name, fastq2_read.seq, fastq2_read.qual])
                fastq_file_name_list.append("{}{}_unknown_R1.fastq"
                                            .format(self.args.WorkingFolder, self.args.Job_Name))
                fastq_file_name_list.append("{}{}_unknown_R2.fastq"
                                            .format(self.args.WorkingFolder, self.args.Job_Name))

        if self.args.Demultiplex:
            self.fastq_compress(list(set(fastq_file_name_list)))

    def main_loop(self):
        """
        Main entry point for repair scar search and processing.
        """

        self.log.info("Beginning main loop|Demultiplexing FASTQ")
        if self.args.PEAR:
            self.consensus_demultiplex()
        else:
            self.demultiplex()

        self.log.info("Spawning {} Jobs to Process {} Libraries".format(self.args.Spawn, len(self.sequence_dict)))
        p = pathos.multiprocessing.Pool(int(self.args.Spawn))

        # My solution for passing key:value pairs to the multiprocessor.  Largest value group goes first.
        data_list = []
        for key in sorted(self.sequence_dict, key=lambda k: len(self.sequence_dict[k]), reverse=True):
            data_list.append([self.log, self.args, self.version, self.run_start, self.target_dict, self.index_dict,
                              key, self.sequence_dict[key]])

        # Not sure if clearing this is really necessary but it is not used again so why keep the RAM tied up.
        self.sequence_dict.clear()

        # Each job is a single instance of the ScarSearch class..
        self.data_output(p.starmap(ScarSearch, data_list))

        self.log.info("Main Loop Finished")

    def dictionary_build(self):
        """
        Build the index dictionary from the index list.
        :return:
        """

        self.log.info("Building DataFrames.")

        # If we are demultiplexing the input FASTQ then setup the output files and dataframe.
        if self.args.Demultiplex:
            self.fastq_outfile_dict = collections.defaultdict(list)
            r1 = FASTQ_Tools.Writer(self.log, "{}{}_unknown_R1.fastq"
                                    .format(self.args.WorkingFolder, self.args.Job_Name))
            r2 = FASTQ_Tools.Writer(self.log, "{}{}_unknown_R2.fastq"
                                    .format(self.args.WorkingFolder, self.args.Job_Name))
            self.fastq_outfile_dict['unknown'] = [r1, r2]

        master_index_dict = {}
        with open(self.args.Master_Index_File) as f:
            for l in f:
                if "#" in l or not l:
                    continue
                l_list = [x for x in l.strip("\n").split("\t")]
                master_index_dict[l_list[0]] = [l_list[1], l_list[2]]

        sample_index_list = Tool_Box.FileParser.indices(self.log, self.args.SampleManifest)
        index_dict = collections.defaultdict(list)

        for sample in sample_index_list:
            index_name = sample[0]

            if index_name in index_dict:
                self.log.error("The index {0} is duplicated.  Correct the error in {1} and try again."
                               .format(sample[0], self.args.SampleManifest))
                raise SystemExit(1)

            sample_name = sample[1]
            sample_replicate = sample[2]
            try:
                target_name = sample[4]
            except IndexError:
                self.log.error("Sample Manifest is missing Target Name column")
                raise SystemExit(1)

            left_index_sequence, right_index_sequence = master_index_dict[index_name]
            index_dict[index_name] = \
                [right_index_sequence.upper(), 0, left_index_sequence.upper(), 0, index_name, sample_name,
                 sample_replicate, target_name]
            '''
            if self.args.Platform == "Illumina":
                left_index_sequence, right_index_sequence = master_index_dict[index_name]
                index_dict[index_name] = \
                    [right_index_sequence.upper(), 0, left_index_sequence.upper(), 0, index_name, sample_name,
                     sample_replicate, target_name]

            elif self.args.Platform == "Ramsden":
                # This is for the Ramsden indexing primers.
                left_index_len = 6
                right_index_len = 6
                left_index_sequence, right_index_sequence = master_index_dict[index_name]

                for left, right in zip(left_index_sequence, right_index_sequence):
                    if left.islower():
                        left_index_len += 1
                    if right.islower():
                        right_index_len += 1

                index_dict[index_name] = \
                    [Sequence_Magic.rcomp(right_index_sequence.upper()), right_index_len, left_index_sequence.upper(),
                     left_index_len, index_name, sample_name, sample_replicate, target_name]

            else:
                self.log.error("Only 'Illumina' or 'Ramsden' --Platform methods currently allowed.")
                raise SystemExit(1)
            '''
            if self.args.Demultiplex:
                r1 = FASTQ_Tools.Writer(self.log, "{}{}_{}_R1.fastq"
                                        .format(self.args.WorkingFolder, self.args.Job_Name, index_name))
                r2 = ""
                if not self.args.PEAR:
                    r2 = FASTQ_Tools.Writer(self.log, "{}{}_{}_R2.fastq"
                                            .format(self.args.WorkingFolder, self.args.Job_Name, index_name))
                self.fastq_outfile_dict[index_name] = [r1, r2]

        return index_dict

    @staticmethod
    def gapped_aligner(log, fasta_data, consensus=False):
        """
        This approach is depreciated.
        Generates and returns a gapped alignment from the given FASTA data using Muscle.
        :param: log
        :param: fasta_data
        :return:
        """
        # Create gapped alignment file in FASTA format using MUSCLE , "-gapopen", "-12"
        # ToDo: would like to control the gap penalties to better find insertions.
        cmd = ['muscle', "-quiet", "-maxiters", "1", "-diags"]
        if consensus:
            cmd = ['muscle', "-quiet", "-maxiters", "1", "-diags"]

        muscle = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)

        output, err = muscle.communicate(input=fasta_data)
        if err:
            log.error(err)
        cat_line = ""
        first_line = True
        gapped_alignment_dict = collections.defaultdict(str)
        key = ""
        list_output = list(output.splitlines())

        for line in list_output:
            if ">" in line:
                if not first_line:
                    gapped_alignment_dict[key] = cat_line
                    cat_line = ""
                first_line = False
                key = line.split(">")[1].strip("\n")
            else:
                cat_line += line.strip("\n")

            gapped_alignment_dict[key] = cat_line

        return gapped_alignment_dict

    def index_matching(self, fastq1_read, fastq2_read=None):
        """
        This matches an index sequence with the index found in the sequence reads.
        :param fastq1_read:
        :param fastq2_read:
        :return:
        """

        match_found = False
        left_seq = ""
        right_seq = ""
        index_key = 'unidentified'
        mismatch = 1
        if self.args.Platform == "Ramsden":
            mismatch = 3

        for index_key in self.index_dict:
            left_index = self.index_dict[index_key][0]
            right_index = self.index_dict[index_key][2]

            if self.args.Platform == "Illumina":
                # The indices are after the last ":" in the header.
                right_match = Sequence_Magic.match_maker(right_index, fastq1_read.name.split(":")[-1].split("+")[0])
                left_match = Sequence_Magic.match_maker(left_index, fastq1_read.name.split(":")[-1].split("+")[1])

            elif self.args.Platform == "Ramsden":
                if self.args.PEAR:
                    left_match = \
                        Sequence_Magic.match_maker(left_index, fastq1_read.seq[-len(left_index):])
                else:
                    left_match = \
                        Sequence_Magic.match_maker(Sequence_Magic.rcomp(left_index), fastq2_read.seq[:len(left_index)])
                right_match = \
                    Sequence_Magic.match_maker(right_index, fastq1_read.seq[:len(right_index)])

            if index_key not in self.read_count_dict:
                self.read_count_dict[index_key] = 0

            if left_match <= mismatch and right_match <= mismatch:
                self.read_count_dict[index_key] += 1
                left_seq = ""
                right_seq = fastq1_read.seq
                match_found = True
                if not fastq2_read:
                    break

            if match_found and fastq2_read:
                # iSeq runs generally have low quality reads on the 3' ends.  This does a blanket trim to remove them.
                left_seq = fastq2_read.seq[:-5]
                right_seq = fastq1_read.seq[:-5]
                break

        if not match_found:
            if 'unidentified' not in self.read_count_dict:
                self.read_count_dict['unidentified'] = 0
            self.read_count_dict['unidentified'] += 1

        return match_found, left_seq, right_seq, index_key, fastq1_read, fastq2_read

    def data_output(self, summary_data_list):
        """
        Format data and write the summary file.
        :param summary_data_list:
        """

        self.log.info("Formatting data and writing summary file")

        summary_file = open("{}{}_ScarMapper_Summary.txt".format(self.args.WorkingFolder, self.args.Job_Name), "w")

        hr_labels = ""
        if self.args.HR_Donor:
            hr_labels = "\tHR Count\tHR Fraction"

        sub_header = \
            "No Junction\tScar Count\tScar Fraction{}\tLeft Deletion Count\tRight Deletion Count\t" \
            "Insertion Count\tMicrohomology Count\tNormalized Microhomology".format(hr_labels)

        phasing_labels = ""
        phase_label_list = []
        for locus in self.phase_count:
            for phase_label in natsort.natsorted(self.phase_count[locus]):
                phasing_labels += "{}\t".format(phase_label)
                phase_label_list.append(phase_label)
            break

        hr_data = ""
        if self.args.HR_Donor:
            hr_data = "HR Donor: {}\n".format(self.args.HR_Donor)

        run_stop = datetime.datetime.today().strftime(self.date_format)
        summary_outstring = "ScarMapper {}\nStart: {}\nEnd: {}\nFASTQ1: {}\nFASTQ2: {}\nReads Analyzed: {}\n{}\n"\
            .format(self.version, self.run_start, run_stop, self.args.FASTQ1, self.args.FASTQ2, self.read_count,
                    hr_data)

        summary_outstring += \
            "Index Name\tSample Name\tSample Replicate\tTarget\tTotal Found\tFraction Total\tPassing Read Filters\t" \
            "Fraction Passing Filters\t{}Consensus Fail\t" \
            "{}\tTsEJ\tNormalized TsEJ\tNHEJ\tNormalized NHEJ\tNon-Microhomology Deletions\tNormalized Non-MH Del\t" \
            "Insertion >=5 +/- Deletions\tNormalized Insertion >=5+/- Deletions\tOther Scar Type\n"\
            .format(phasing_labels, sub_header)

        '''
        The data_list contains information for each library.  [0] index name; [1] reads passing all 
        filters; [2] reads with a left junction; [3] reads with a right junction; [4] reads with an insertion;
        [5] reads with microhomology; [6] reads with no identifiable cut; [7] filtered reads [8] scar type list.
        '''

        for data_list in summary_data_list:    
            index_name = data_list.summary_data[0]
            sample_name = self.index_dict[index_name][5]
            sample_replicate = self.index_dict[index_name][6]
            library_read_count = self.read_count_dict[index_name]
            fraction_all_reads = library_read_count/self.read_count
            passing_filters = data_list.summary_data[1]
            fraction_passing = passing_filters/library_read_count
            left_del = data_list.summary_data[2]
            right_del = data_list.summary_data[3]
            total_ins = data_list.summary_data[4]
            microhomology = data_list.summary_data[5]
            cut = passing_filters-data_list.summary_data[6][1]-data_list.summary_data[6][0]
            target = data_list.summary_data[9]
            phase_key = "{}+{}".format(index_name, target)

            phase_data = ""
            for phase in natsort.natsorted(self.phase_count[phase_key]):
                phase_data += "{}\t".format(self.phase_count[phase_key][phase]/library_read_count)

            no_junction = data_list.summary_data[6][0]
            con_fail = data_list.summary_data[7][1]

            try:
                cut_fraction = cut/passing_filters
            except ZeroDivisionError:
                cut_fraction = 'nan'

            # Process HR data if present
            hr_data = ""
            if self.args.HR_Donor:
                hr_count = "{}; {}".format(data_list.summary_data[10][0], data_list.summary_data[10][1])
                hr_frequency = sum(data_list.summary_data[10])/passing_filters
                hr_data = "\t{}\t{}".format(hr_count, hr_frequency)

            try:
                tmej = data_list.summary_data[8][0]
            except TypeError:
                continue
            nhej = data_list.summary_data[8][1]
            non_microhomology_del = data_list.summary_data[8][4]
            large_ins = data_list.summary_data[8][2]
            other_scar = data_list.summary_data[8][3]

            if cut == 0:
                microhomology_fraction = 'nan'
                non_mh_del_fraction = 'nan'
                large_ins_fraction = 'nan'
                nhej_fraction = 'nan'
                tmej_fraction = 'nan'
            else:
                microhomology_fraction = microhomology / cut
                non_mh_del_fraction = non_microhomology_del / cut
                large_ins_fraction = large_ins / cut
                nhej_fraction = nhej / cut
                tmej_fraction = tmej / cut

            summary_outstring += \
                "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}{}\t{}\t{}\t{}{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t" \
                "{}\t{}\n"\
                .format(index_name, sample_name, sample_replicate, target, library_read_count, fraction_all_reads,
                        passing_filters, fraction_passing, phase_data, con_fail,
                        no_junction, cut, cut_fraction, hr_data, left_del, right_del, total_ins,
                        microhomology, microhomology_fraction, tmej, tmej_fraction, nhej, nhej_fraction,
                        non_microhomology_del, non_mh_del_fraction, large_ins, large_ins_fraction, other_scar)

        summary_outstring += "\nUnidentified\t{}\t{}" \
            .format(self.read_count_dict["unidentified"], self.read_count_dict["unidentified"] / self.read_count)

        summary_file.write(summary_outstring)
        summary_file.close()
