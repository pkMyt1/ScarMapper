# -*- coding: utf-8 -*-
"""

@author: Dennis A. Simpson
         University of North Carolina at Chapel Hill
         Chapel Hill, NC  27599
@copyright: 2020
"""

import csv
import os
import re
import numpy
from Valkyries import Tool_Box
from contextlib import suppress

__author__ = 'Dennis A. Simpson'
__version__ = "0.6.2"


def indices(input_file):
    """
    Parse the index file or target file and return a list of values.
    :return:
    """

    index_list = []
    line_num = 0
    index_file = list(csv.reader(open(input_file), delimiter='\t'))
    for line in index_file:
        line_num += 1
        col_count = len(line)

        if col_count > 1 and len(line[0].split("#")[0]) > 1:  # Skip any lines that are blank or comments.
            tmp_line = []
            for i in range(col_count):
                try:
                    line[i] = line[i].split("#")[0]  # Strip out end of line comments and white space.
                except IndexError:
                    raise SystemExit(
                        "There is a syntax error in file {0} on line {1}, column {2} "
                            .format(input_file, str(line_num), str(i)))

                line[i] = re.sub(",", '', line[i])  # Strip out any commas.

                tmp_line.append(line[i])
            index_list.append(tmp_line)

    return index_list


class IndexFileParser:
    """
    This is a file parser to deal with imputing indices or ploidy data.
    """

    def __init__(self, input_file, segment_copy=False, target_bed=False):

        if segment_copy:
            self.input_file = input_file.seg_copy_file
            self.chrY = input_file.chrY
        elif target_bed:
            self.input_file = input_file.target_bed_file
            self.chrY = input_file.chrY

        if not os.path.isfile(self.input_file):
            raise SystemExit(self.input_file + " not found.  Check file name and path in run_Volundr and try again.")

    def interval_file(self):
        """
        Parse the index file and return a list of indices.
        :return:
        """

        index_list = []
        line_num = 0
        index_file = list(csv.reader(open(self.input_file), delimiter='\t'))
        for line in index_file:
            line_num += 1
            col_count = len(line)

            if col_count > 1 and len(line[0].split("#")[0]) > 1:  # Skip any lines that are blank or comments.
                tmp_line = []
                for i in range(col_count):
                    try:
                        line[i] = line[i].split("#")[0]  # Strip out end of line comments and white space.
                    except IndexError:
                        raise SystemExit(
                            "There is a syntax error in file {0} on line {1}, column {2} "
                                .format(self.input_file, str(line_num), str(i)))

                    line[i] = re.sub(",", '', line[i])  # Strip out any commas.

                    tmp_line.append(line[i])
                index_list.append(tmp_line)

        return index_list

    def array_builder(self):
        initial_array = numpy.genfromtxt(self.input_file, delimiter='\t', dtype=object)

        array_index_list = []

        for i in range(initial_array.shape[0]):
            array_index_list.append([i])

        '''Append index column to initial array.'''
        final_array = numpy.append(array_index_list, numpy.asarray(initial_array), axis=1)

        array_index_list.clear()

        return final_array

    def seg_count_file(self):
        """
        This function parses the tab delimited SegCopy file into a complex dictionary.
        :return:
        """

        prior_ploidy = {}  # This is essentially a tracking dictionary that I make here because the keys are available.
        bin_tracking_dict = Tool_Box.VivifiedDictionary()
        line_num = 0
        seg_copy_array = self.array_builder()

        seg_count = list(csv.reader(open(self.input_file), delimiter='\t'))

        for line in seg_count:
            if line_num > 0:
                bin_tracking_dict[line[0]][line_num] = (line[1], line[2])

            elif line_num == 0:  # First line is the header.
                label_list = line
                for i in range(len(label_list)):
                    if i > 2:
                        prior_ploidy[label_list[i]] = [-1, False, 0, 0, 0]
            line_num += 1

        if not eval(self.chrY):
            with suppress(KeyError):
                bin_tracking_dict.pop("chrY")

        return prior_ploidy, bin_tracking_dict, seg_copy_array
