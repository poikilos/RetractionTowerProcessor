﻿#!/usr/bin/env python
'''
Retraction Tower Processor
--------------------------
This program processes template gcode to produce RetractionTower.gcode.
https://github.com/poikilos/RetractionTowerProcessor is a
Python reinterpretation of logiclrd's C# RetractionTestTowersGCodeGenerator.

Usage:
run.py [<path>] [options]

If path is specified, the output file will be named the same except
with "RetractionTest " prepended (or replacing the word "Template" if
present in the name).

Options:
/output  <path>            Specify where to save the gcode
                           (default: RetractionTower.gcode).
/center <x> <y>            Set the middle for calculating extents.
/template <path>           Choose an input gcode file (This option is
                           for backward compatibility. The first
                           argument without a command switch also sets
                           the template).
/startwith <retraction>    Start with this retraction length (default 2).
/setat <z>                 Keep the same retraction up to here (default 2).
/interpolateto <z> <retr.> Interpolate up to here and to this retraction
                           (default z=32,
                           default retraction startwith + .5 per mm).
/interpolate <retr.>       Interpolate up to this retraction
                           (to z=32).
/checkfile                 Check the file only.
--debug or /debug          Show every retraction at every height.
'''
# Processed by pycodetool https://github.com/poikilos/pycodetool
# 2021-03-14 10:52:20
# from System import *
# from System.Collections.Generic import *
# from System.IO import *
# from System.Linq import *
import sys
import os
import shutil
from retractiontower.fxshim import (
    IsWhiteSpace,
    decimal_Parse,
    IsNullOrWhiteSpace,
    IsDigit,
)
from retractiontower.gcodecommand import GCodeCommand
from retractiontower.gcodecommandpart import GCodeCommandPart


verbosity = 0
verbosities = [True, False, 0, 1, 2]


def set_verbosity(level):
    global verbosity
    if level not in verbosities:
        raise ValueError("level must be one of {}".format(verbosities))
    verbosity = level


def echo0(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    return True


def echo1(*args, **kwargs):
    if verbosity < 1:
        return False
    print(*args, file=sys.stderr, **kwargs)
    return True


def echo2(*args, **kwargs):
    if verbosity < 2:
        return False
    print(*args, file=sys.stderr, **kwargs)
    return True


def usage():
    print(__doc__)


def isfloat(num):
    try:
        float(num)
        return True
    except ValueError:
        return False


def peek_line(f):
    # as per <https://stackoverflow.com/a/16840747/4541104>
    pos = f.tell()
    line = f.readline()
    f.seek(pos)
    return line


def limited_f(f, places=4):
    places = int(places)
    return (("{:."+str(places)+"f}").format(f)).rstrip("0").rstrip(".")


class Extent:
    def __init__(self):
        self.From = 0.0
        self.To = 0.0

    @property
    def Middle(self):
        return self.From * 0.5 + self.To * 0.5

    def Extend(self, value, tbs=None):
        '''
        Keyword arguments:
        tbs -- a string to display on error (put the line of G-code or
               something else useful in here as some form of traceback
               string).
        '''
        if isinstance(value, int):
            msg = "."
            if tbs is not None:
                msg = " while \"{}\".".format(tbs)
            print("Warning: The value should be an float but"
                  " is \"{}\"".format(value) + msg)
            value = float(value)
        if not isinstance(value, float):
            msg = "."
            if tbs is not None:
                msg = " while \"{}\".".format(tbs)
            raise ValueError("The value must be an float but"
                             " is \"{}\"".format(value) + msg)
        if value < self.From:
            self.From = value
        if value > self.To:
            self.To = value


# enum CurvePointType
class CurvePointType:
    SameValueUntil = 0
    InterpolateUpTo = 1


class CurvePoint:
    '''
    members:
    PointType -- a value that matches a constant in CurvePointType
    '''
    def __init__(self, **kwargs):
        self.PointType = kwargs.get('PointType')
        self.Z = kwargs.get('Z')
        self.Retraction = kwargs.get('Retraction')

    @staticmethod
    def compare(curvepoint1, curvepoint2):
        return curvepoint1.Z - curvepoint2.Z

    def __lt__(self, other):
        return CurvePoint.compare(self, other) < 0

    def __gt__(self, other):
        return CurvePoint.compare(self, other) > 0

    def __eq__(self, other):
        return CurvePoint.compare(self, other) == 0

    def __le__(self, other):
        return CurvePoint.compare(self, other) <= 0

    def __ge__(self, other):
        return CurvePoint.compare(self, other) >= 0

    def __ne__(self, other):
        return CurvePoint.compare(self, other) != 0


class GCodeWriter:
    def __init__(self, underlying):
        self._underlying = underlying
        self.NumLines = 0
        self.NumCommands = 0
        self.NumMovementCommands = 0
        self.NumCharactersWritten = 0

    def WriteLine(self, command):
        if isinstance(command, GCodeCommand):
            command = command.ToString()
        self.NumLines += 1
        if GCodeWriter.IsCommand(command):
            self.NumCommands += 1
            if GCodeWriter.IsMovementCommand(command):
                self.NumMovementCommands += 1
        self.NumCharactersWritten += len(command) + len(os.linesep)
        self._underlying.write(command + "\n")

    @staticmethod
    def IsCommand(line):
        i = -1
        while i + 1 < len(line):
            i += 1
            if IsWhiteSpace(line, i):
                continue
            if GCodeCommandPart.isCommentAt(line, i):
                break
            if (line[i] == 'G') or (line[i] == 'M'):
                i += 1
                if i >= len(line):
                    return False
                return IsDigit(line[i])
        return False

    @staticmethod
    def IsMovementCommand(command):
        i = -1
        while i + 1 < len(command):
            i += 1
            if IsWhiteSpace(command, i):
                continue
            if GCodeCommandPart.isCommentAt(command, i):
                break
            if command[i] == 'G':
                i += 1
                if i >= len(command):
                    return False
                if (command[i] != '0') and (command[i] != '1'):
                    return False
                i += 1
                return (i >= len(command)) or IsWhiteSpace(command[i])
        return False


class Program:
    _FirstTowerZ = 2.1
    _GraphRowHeight = 0.5
    _DEFAULT_TEMPLATE_NAME = "Template.gcode"
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
    MODEL_PATH = os.path.join(DATA_DIR, "Model",
                              "RetractionTestCylinders.stl")
    # TEMPLATE_PATH = os.path.join(DATA_DIR, _DEFAULT_TEMPLATE_NAME)
    TEMPLATE_PATH = os.path.join(os.getcwd(), _DEFAULT_TEMPLATE_NAME)
    _extents = None
    _extents_done = False

    @staticmethod
    def get_FirstTowerZ():
        return Program._FirstTowerZ

    @staticmethod
    def get_GraphRowHeight():
        return Program._GraphRowHeight

    @staticmethod
    def getTemplateUsage():
        msg = (
             "You must specify a template path or\n"
             " generate the G-code file \"{}\""
             " from \"{}\" using your slicer software."
             "".format(Program.TEMPLATE_PATH,
                       Program.MODEL_PATH)
        )
        return msg

    @staticmethod
    def GetTemplateReader():
        # formerly (nameof(RetractionTestTowersGCodeGenerator)
        # + ".Retraction Test Towers Template.gcode"))
        if not os.path.isfile(Program.TEMPLATE_PATH):
            raise ValueError(Program.getTemplateUsage())
            # return None
        return open(Program.TEMPLATE_PATH)

    @staticmethod
    def MeasureGCode(stream, path=None):
        #  Count only G1 moves in X and Y.
        #  Count G0 and G1 moves in Z, but only for Z values
        #    where filament is extruded.
        x = Extent()
        y = Extent()
        z = Extent()

        x.From = y.From = z.From = sys.float_info.max
        x.To = y.To = z.To = sys.float_info.min
        lastE = sys.float_info.min
        currentZ = sys.float_info.min
        zTBS = None
        line_n = 0
        while True:
            line_n += 1  # Counting numbers start at 1.
            line = stream.readline()
            if not line:
                break
            line = line.rstrip("\n\r")

            if IsNullOrWhiteSpace(line):
                continue
            command = GCodeCommand(line, path=path, line_n=line_n)

            if command.Command == "G1":
                if command.HasParameter('X'):
                    x.Extend(
                        command.GetParameter('X'),
                        tbs="on X where line=\"{}\"".format(line)
                    )
                if command.HasParameter('Y'):
                    y.Extend(
                        command.GetParameter('Y'),
                        tbs="on Y where line=\"{}\"".format(line)
                    )

            if (command.Command == "G0") or (command.Command == "G1"):
                if command.HasParameter('Z'):
                    currentZ = command.GetParameter('Z')
                    zTBS = ("z originated from line=\"{}\""
                            "".format(line))

                if command.HasParameter('E'):
                    e = command.GetParameter('E')

                    if (e > lastE) and (currentZ != sys.float_info.min):
                        lastE = e
                        z.Extend(
                            currentZ,
                            tbs=("on Z where line=\"{}\" and {}"
                                 "").format(line, zTBS)
                        )

        class AnonymousClass:
            pass
        result = AnonymousClass()
        result.X = x
        result.Y = y
        result.Z = z
        return result

    @classmethod
    def CalculateExtents(cls):
        path = cls.TEMPLATE_PATH
        if not os.path.isfile(path):
            return False
        cls._extents_done = False
        reader = cls.GetTemplateReader()
        try:
            cls._extents = cls.MeasureGCode(reader, path=path)
            cls._extents_done = True
        finally:
            reader.close()

        if cls._extents_done:
            print("Template extents:")

            print("    From     Centre   To")
            print("X   {0: >5.1f}    {1: >5.1f}    {2: >5.1f}"
                  "".format(cls._extents.X.From, cls._extents.X.Middle,
                            cls._extents.X.To))
            print("Y   {0: >5.1f}    {1: >5.1f}    {2: >5.1f}"
                  "".format(cls._extents.Y.From, cls._extents.Y.Middle,
                            cls._extents.Y.To))
            print("Z   {0: >5.1f}    {1: >5.1f}    {2: >5.1f}"
                  "".format(cls._extents.Z.From, cls._extents.Z.Middle,
                            cls._extents.Z.To))
        return cls._extents_done

    @classmethod
    def set_template(cls, template_path):
        cls.TEMPLATE_PATH = template_path
        cls.CalculateExtents()
        if cls.extents_used_by == "/center":
            deltaX = center[0] - cls._extents.X.Middle
            deltaY = center[1] - cls._extents.Y.Middle
        elif cls.extents_used_by is not None:
            raise NotImplementedError(
                "The program cannot recalculate the"
                " effect of {} after changing the model."
            )

    @classmethod
    def Main(cls, args):
        cls.CalculateExtents()

        curvePoints = []

        deltaX = 0.0
        deltaY = 0.0

        outputFileName = "RetractionTest.gcode"
        inputFileName = None
        cls.extents_used_by = None
        center = None
        last_retraction = 2.0
        default_height = 32.0
        start_point_done = False
        start_point_done_flags = ["/startwith", "/setat"]
        prevArgName = None
        if True:
            index = 0

            while index < len(args):
                curvePoint = CurvePoint()

                argName = args[index].lower()
                if prevArgName in start_point_done_flags:
                    start_point_done = True
                prevArgName = argName
                if argName == "/output":
                    outputFileName = args[index + 1]
                    index += 2
                    continue

                elif argName == "/center":
                    if not cls._extents_done:
                        raise ValueError(Program.getTemplateUsage())
                    center = (float(args[index + 1]),
                              float(args[index + 2]))
                    deltaX = center[0] - cls._extents.X.Middle
                    deltaY = center[1] - cls._extents.Y.Middle
                    index += 3
                    cls.extents_used_by = "/center"
                    continue

                elif argName == "/template":
                    inputFileName = args[index+1]
                    cls.set_template(inputFileName)
                    index += 2
                    continue

                elif argName == "/startwith":
                    initialPoint = CurvePoint()
                    initialPoint.PointType = CurvePointType.SameValueUntil
                    initialPoint.Z = cls.get_FirstTowerZ()
                    initialPoint.Retraction = float(args[index + 1])
                    curvePoints.append(initialPoint)
                    index += 2
                    continue

                elif ((argName == "/setat")
                        or (argName == "/interpolateto")
                        or (argName == "/interpolate")):
                    if argName == "/setat":
                        curvePoint.PointType = CurvePointType.SameValueUntil
                    else:
                        curvePoint.PointType = CurvePointType.InterpolateUpTo
                        # prevent divide by zero:
                        if not start_point_done:
                            echo0("Error: You must use one of {} first."
                                  "".format(start_point_done_flags))
                            return 1

                    if argName == "/setat":
                        curvePoint.Z = float(args[index + 1])
                        curvePoint.Retraction = last_retraction
                        index += 2
                    elif argName == "/interpolateto":
                        curvePoint.Z = float(args[index + 1])
                        if len(args) <= index + 2:
                            usage()
                            echo0("Error: You must specify both"
                                  " <height> and <Retraction>. If you only"
                                  " want to set retraction"
                                  " (assuming Z {} as the height),"
                                  " use /interpolate."
                                  "".format(default_height))
                            return 1
                        if not isfloat(args[index + 2]):
                            usage()
                            echo0('Error: You must specify a number'
                                  ' for Retraction after Z but "{}"'
                                  ' is not a number.'.format(args[index + 2]))
                            return 1
                        curvePoint.Retraction = float(args[index + 2])
                        last_retraction = curvePoint.Retraction
                        index += 3
                    elif argName == "/interpolate":
                        curvePoint.Z = default_height
                        curvePoint.Retraction = float(args[index + 1])
                        last_retraction = curvePoint.Retraction
                        index += 2
                    else:
                        raise NotImplementedError(argName)
                    curvePoints.append(curvePoint)
                    continue

                elif argName == "/checkfile":
                    cls.AnalyzeFile(args[index + 1])
                    index += 2
                    return 0
                elif argName in ["--help", "/?"]:
                    usage()
                    return 0
                elif argName in ["--debug", "/debug"]:
                    set_verbosity(2)
                    index += 1
                    continue
                elif argName in ["--verbose", "/verbose", "/v", "-v"]:
                    set_verbosity(1)
                    index += 1
                    continue

                elif inputFileName is None:
                    # must be the *last* case
                    inputFileName = args[index]
                    cls.set_template(inputFileName)
                    index += 1
                    continue

                raise Exception(
                    'Error: "{}" is not a valid argument'.format(argName)
                )
        if (not os.path.isfile(cls.TEMPLATE_PATH)
                or (not cls._extents_done)):
            raise ValueError(Program.getTemplateUsage())
        if len(curvePoints) == 0:
            curvePoints.append(
                CurvePoint(
                    PointType=CurvePointType.SameValueUntil,
                    Z=cls.get_FirstTowerZ(),
                    Retraction=2.0,
                )
            )

            curvePoints.append(
                CurvePoint(
                    PointType=CurvePointType.InterpolateUpTo,
                    Z=cls._extents.Z.To,
                    Retraction=3.0,
                )
            )

        print("")
        inputFileName = cls.TEMPLATE_PATH
        print('Using "{}"'.format(inputFileName))
        if "Template" in inputFileName:
            outputFileName = inputFileName.replace("Template",
                                                   "RetractionTest")
        else:
            outputFileName = "RetractionTest " + inputFileName
        # ^ The name changes more after spans are calculated below.


        if (deltaX != 0) or (deltaY != 0):
            print(
                "Will translate test print to be centered at ({0:.1f}"
                ", {1:.1f})".format(
                    cls._extents.X.Middle + deltaX,
                    cls._extents.Y.Middle + deltaY,
                )
            )
            print("")

        print("Z    ? Retraction")

        lastCurvePointsPassed = 0

        # z = 17.0  # for original
        z = default_height
        span = cls.get_FirstTowerZ() - cls.get_GraphRowHeight()
        pairs = []
        while z >= span:
            lastExtraRow = False
            if z < cls.get_FirstTowerZ():
                lastExtraRow = True
                z = cls.get_FirstTowerZ()
            sys.stdout.write("{:.1f}".format(z).rjust(4))
            sys.stdout.write(' ')
            curvePointsPassed = \
                sum(1 for point in curvePoints if point.Z >= z)
            if curvePointsPassed == lastCurvePointsPassed:
                sys.stdout.write("  ")
            else:
                sys.stdout.write("+ ")
                lastCurvePointsPassed = curvePointsPassed
            retraction = cls.GetRetractionForZ(z, curvePoints)
            sys.stdout.write("{:.4f} ".format(retraction).rjust(8))
            barWidth = int(round(retraction * 5))
            sys.stdout.write('*'*barWidth)
            print("")
            if lastExtraRow:
                break
            z -= cls.get_GraphRowHeight()

        print("")
        # print('Will write output to: "{0}"'.format(outputFileName))
        # ^ The name is not finalized yet.
        newFileName = None
        with open(outputFileName, 'w') as writer:
            print("")
            print("Generating G code...")

            pairs = cls.TranslateGCode(
                cls.GetTemplateReader(),
                writer,
                cls.get_FirstTowerZ(),
                deltaX,
                deltaY,
                curvePoints,
            )
            left, dotExt = os.path.splitext(outputFileName)
            left += " ("
            left += "z={},r={}".format(
                limited_f(pairs[0][0]),
                limited_f(pairs[0][1])
            )
            # if len(pairs) > 2:
            left += " to z={},r={}".format(
                limited_f(pairs[-1][0]),
                limited_f(pairs[-1][1])
            )
            left += ")"
            newFileName = left + dotExt
        if newFileName is not None:
            shutil.move(outputFileName, newFileName)
            outputFileName = newFileName

        print("")
        print('* wrote "{}"'.format(os.path.abspath(outputFileName)))
        return 0

    @staticmethod
    def AnalyzeFile(fileName):
        with open(fileName, 'r') as reader:
            z = sys.float_info.min
            lastE = sys.float_info.min

            while True:
                line = reader.readline()
                if not line:
                    break
                line = line.rstrip("\n\r")

                command = GCodeCommand(line)

                if (command.Command == "G0") or (command.Command == "G1"):
                    if command.HasParameter('Z'):
                        z = command.GetParameter('Z')

                    if command.HasParameter('E'):
                        e = command.GetParameter('E')

                        if e < lastE:
                            print("=> Retract by {0} at Z {z}"
                                  "".format(lastE-e, z=z))
                        else:
                            lastE = e
        return 0

    @staticmethod
    def TranslateGCode(reader, writer, firstTowerZ, deltaX, deltaY,
                       curvePoints):
        '''
        Returns:
        a list of (retraction, z) tuples, where the first is the first
        value, the second is the first one where retraction differs,
        and the last is the last set changed.
        '''
        if not isinstance(firstTowerZ, float):
            raise ValueError("The firstTowerZ must be an float but"
                             " is \"{}\".".format(firstTowerZ))
        if not isinstance(curvePoints, list):
            raise ValueError("The curvePoints must be a list but"
                             " is \"{}\".".format(curvePoints))

        curvePoints = sorted(curvePoints)
        z = sys.float_info.min
        uniqueZValues = set()
        lastE = sys.float_info.min
        lastSerialMessage = ""
        gcodeWriter = GCodeWriter(writer)
        numberOfRetractions = 0
        pairs = []
        line_n = 0
        is_relative = False
        while True:
            line_n += 1
            line = reader.readline()
            if not line:
                break
            line = line.rstrip("\n\r")

            command = GCodeCommand(line)

            if (command.Command == "G0") or (command.Command == "G1"):
                if command.HasParameter('X'):
                    command.SetParameter(
                        'X',
                        command.GetParameter('X') + deltaX
                    )
                if command.HasParameter('Y'):
                    command.SetParameter(
                        'Y',
                        command.GetParameter('Y') + deltaY
                    )

                if command.HasParameter('Z'):
                    z = command.GetParameter('Z')

                    if uniqueZValues.add(z):
                        sys.stdout.write('#')

                if z >= firstTowerZ:
                    if command.HasParameter('E'):
                        e = command.GetParameter('E')

                        if e < lastE:
                            #  Retraction!
                            numberOfRetractions += 1

                            retraction = Program.GetRetractionForZ(
                                z,
                                curvePoints
                            )
                            if is_relative:
                                # Don't change relative extrusion
                                #   such as end G-code.
                                newE = retraction
                                if e < 0:
                                    newE *= -1.0
                            else:
                                newE = lastE - retraction
                            command.SetParameter('E', newE)
                            echo2("* z={:.2f},r={:.4f}".format(z, retraction))
                            if len(pairs) == 0:
                                pairs.append((z, retraction))
                            elif len(pairs) == 1:
                                # Write the first delta if there is a
                                #   delta.
                                if retraction != pairs[0][0]:
                                    pairs.append((z, retraction))
                            else:
                                # Always overwrite the third element,
                                #   which represents the last value,
                                #   unless negative (end retraction)
                                if len(pairs) < 3:
                                    pairs.append((z, retraction))
                                else:
                                    pairs[2] = (z, retraction)
                            lcdScreenMessage = (
                                "dE {retraction:.3f} at Z {z:.1f}"
                            ).format(retraction=retraction, z=z)
                            serialMessage = (
                                "Retraction {retraction:.5f}"
                                " at Z {z:.1f}"
                            ).format(retraction=retraction, z=z)

                            gcodeWriter.WriteLine("M117 " + lcdScreenMessage)

                            if serialMessage != lastSerialMessage:
                                gcodeWriter.WriteLine("M118 " + serialMessage)

                                lastSerialMessage = serialMessage

                        lastE = e
            elif command.Command == "G91":
                is_relative = True
            elif command.Command == "G90":
                is_relative = False

            gcodeWriter.WriteLine(command)

        print("")
        print("")
        print("See the chart generated above for what measurement (from bottom, not top of base) demonstrates what amount of retraction.")
        print("")
        print("Output:")
        print("- {0} characters".format(gcodeWriter.NumCharactersWritten))
        print("- {0} lines".format(gcodeWriter.NumLines))
        print("- {0} commands".format(gcodeWriter.NumCommands))
        print("- {0} movement commands".format(gcodeWriter.NumMovementCommands))
        print("- {0} unique Z values".format(len(uniqueZValues)))
        print("- {0} retractions".format(numberOfRetractions))
        return pairs

    @staticmethod
    def GetRetractionForZ(z, curvePoints):
        if isinstance(z, int):
            msg = "."
            if tbs is not None:
                msg = " while \"{}\".".format(tbs)
            print("Warning: The z should be an float but"
                  " is \"{}\"".format(z) + msg)
            z = float(z)
        if not isinstance(z, float):
            raise ValueError("The z must be an float but"
                             " is \"{}\".".format(z))
        if not isinstance(curvePoints, list):
            raise ValueError("The curvePoints must be a list but"
                             " is \"{}\".".format(curvePoints))
        previousPoint = curvePoints[0]

        for point in curvePoints:
            if point.Z >= z:
                if point.PointType == CurvePointType.SameValueUntil:
                    return previousPoint.Retraction

                interpolateFrom = previousPoint.Retraction
                interpolateTo = point.Retraction

                interpolateFromZ = previousPoint.Z
                interpolateToZ = point.Z

                interpolateRange = interpolateToZ - interpolateFromZ

                weightTo = (z - previousPoint.Z) / interpolateRange
                weightFrom = (point.Z - z) / interpolateRange

                result = interpolateFrom * weightFrom + interpolateTo * weightTo
                if result > interpolateTo:
                    print(
                        'Warning: result {} > interpolateTo {}'
                        ' (interpolateRange={}, interpolateToZ={}, z={},'
                        ' weightFrom={}, weightTo={})'
                        ''.format(result, interpolateTo, interpolateRange,
                                  interpolateToZ, z, weightFrom, weightTo)
                    )

                return result

            previousPoint = point
        return curvePoints[-1].Retraction


def main():
    return Program.Main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
