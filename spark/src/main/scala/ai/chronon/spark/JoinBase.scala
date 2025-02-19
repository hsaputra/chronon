/*
 *    Copyright (C) 2023 The Chronon Authors.
 *
 *    Licensed under the Apache License, Version 2.0 (the "License");
 *    you may not use this file except in compliance with the License.
 *    You may obtain a copy of the License at
 *
 *        http://www.apache.org/licenses/LICENSE-2.0
 *
 *    Unless required by applicable law or agreed to in writing, software
 *    distributed under the License is distributed on an "AS IS" BASIS,
 *    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *    See the License for the specific language governing permissions and
 *    limitations under the License.
 */

package ai.chronon.spark

import ai.chronon.api
import ai.chronon.api.DataModel.{Entities, Events}
import ai.chronon.api.Extensions._
import ai.chronon.api.{Accuracy, Constants, JoinPart}
import ai.chronon.online.Metrics
import ai.chronon.spark.Extensions._
import ai.chronon.spark.JoinUtils.{coalescedJoin, leftDf, tablesToRecompute}
import com.google.gson.Gson
import org.apache.spark.sql.DataFrame
import org.apache.spark.sql.functions._
import org.apache.spark.util.sketch.BloomFilter
import org.slf4j.LoggerFactory
import java.time.Instant

import scala.collection.JavaConverters._
import scala.collection.Seq
import scala.util.ScalaJavaConversions.ListOps

abstract class JoinBase(joinConf: api.Join,
                        endPartition: String,
                        tableUtils: TableUtils,
                        skipFirstHole: Boolean,
                        mutationScan: Boolean = true,
                        showDf: Boolean = false,
                        selectedJoinParts: Option[Seq[String]] = None) {
  @transient lazy val logger = LoggerFactory.getLogger(getClass)
  assert(Option(joinConf.metaData.outputNamespace).nonEmpty, s"output namespace could not be empty or null")
  val metrics: Metrics.Context = Metrics.Context(Metrics.Environment.JoinOffline, joinConf)
  private val outputTable = joinConf.metaData.outputTable
  // Get table properties from config
  protected val confTableProps: Map[String, String] = Option(joinConf.metaData.tableProperties)
    .map(_.asScala.toMap)
    .getOrElse(Map.empty[String, String])

  private val gson = new Gson()
  // Combine tableProperties set on conf with encoded Join
  protected val tableProps: Map[String, String] =
    confTableProps ++ Map(Constants.SemanticHashKey -> gson.toJson(joinConf.semanticHash.asJava))

  def joinWithLeft(leftDf: DataFrame, rightDf: DataFrame, joinPart: JoinPart): DataFrame = {
    val partLeftKeys = joinPart.rightToLeft.values.toArray

    // compute join keys, besides the groupBy keys -  like ds, ts etc.,
    val additionalKeys: Seq[String] = {
      if (joinConf.left.dataModel == Entities) {
        Seq(tableUtils.partitionColumn)
      } else if (joinPart.groupBy.inferredAccuracy == Accuracy.TEMPORAL) {
        Seq(Constants.TimeColumn, tableUtils.partitionColumn)
      } else { // left-events + snapshot => join-key = ds_of_left_ts
        Seq(Constants.TimePartitionColumn)
      }
    }
    val keys = partLeftKeys ++ additionalKeys

    // apply prefix to value columns
    val nonValueColumns = joinPart.rightToLeft.keys.toArray ++ Array(Constants.TimeColumn,
                                                                     tableUtils.partitionColumn,
                                                                     Constants.TimePartitionColumn)
    val valueColumns = rightDf.schema.names.filterNot(nonValueColumns.contains)
    val prefixedRightDf = rightDf.prefixColumnNames(joinPart.fullPrefix, valueColumns)

    // apply key-renaming to key columns
    val newColumns = prefixedRightDf.columns.map { column =>
      if (joinPart.rightToLeft.contains(column)) {
        col(column).as(joinPart.rightToLeft(column))
      } else {
        col(column)
      }
    }
    val keyRenamedRightDf = prefixedRightDf.select(newColumns: _*)

    // adjust join keys
    val joinableRightDf = if (additionalKeys.contains(Constants.TimePartitionColumn)) {
      // increment one day to align with left side ts_ds
      // because one day was decremented from the partition range for snapshot accuracy
      keyRenamedRightDf
        .withColumn(
          Constants.TimePartitionColumn,
          date_format(date_add(to_date(col(tableUtils.partitionColumn), tableUtils.partitionSpec.format), 1),
                      tableUtils.partitionSpec.format)
        )
        .drop(tableUtils.partitionColumn)
    } else {
      keyRenamedRightDf
    }

    logger.info(s"""
               |Join keys for ${joinPart.groupBy.metaData.name}: ${keys.mkString(", ")}
               |Left Schema:
               |${leftDf.schema.pretty}
               |Right Schema:
               |${joinableRightDf.schema.pretty}""".stripMargin)
    val joinedDf = coalescedJoin(leftDf, joinableRightDf, keys)
    logger.info(s"""Final Schema:
               |${joinedDf.schema.pretty}
               |""".stripMargin)

    joinedDf
  }

  def computeRightTable(leftDf: Option[DfWithStats],
                        joinPart: JoinPart,
                        leftRange: PartitionRange,
                        joinLevelBloomMapOpt: Option[Map[String, BloomFilter]],
                        smallMode: Boolean = false): Option[DataFrame] = {

    val partTable = joinConf.partOutputTable(joinPart)
    val partMetrics = Metrics.Context(metrics, joinPart)
    if (joinPart.groupBy.aggregations == null) {
      // for non-aggregation cases, we directly read from the source table and there is no intermediate join part table
      computeJoinPart(leftDf, joinPart, joinLevelBloomMapOpt, smallMode)
    } else {
      // in Events <> batch GB case, the partition dates are offset by 1
      val shiftDays =
        if (joinConf.left.dataModel == Events && joinPart.groupBy.inferredAccuracy == Accuracy.SNAPSHOT) {
          -1
        } else {
          0
        }
      val rightRange = leftRange.shift(shiftDays)
      try {
        val unfilledRanges = tableUtils
          .unfilledRanges(
            partTable,
            rightRange,
            Some(Seq(joinConf.left.table)),
            inputToOutputShift = shiftDays,
            // never skip hole during partTable's range determination logic because we don't want partTable
            // and joinTable to be out of sync. skipping behavior is already handled in the outer loop.
            skipFirstHole = false
          )
          .getOrElse(Seq())

        val unfilledRangeCombined = if (!unfilledRanges.isEmpty && smallMode) {
          // For small mode we want to "un-chunk" the unfilled ranges, because left side can be sparse
          // in dates, and it often ends up being less efficient to run more jobs in an effort to
          // avoid computing unnecessary left range. In the future we can look for more intelligent chunking
          // as an alternative/better way to handle this.
          Seq(PartitionRange(unfilledRanges.minBy(_.start).start, unfilledRanges.maxBy(_.end).end)(tableUtils))
        } else {
          unfilledRanges
        }

        val partitionCount = unfilledRangeCombined.map(_.partitions.length).sum
        if (partitionCount > 0) {
          val start = System.currentTimeMillis()
          unfilledRangeCombined
            .foreach(unfilledRange => {
              val leftUnfilledRange = unfilledRange.shift(-shiftDays)
              val prunedLeft = leftDf.flatMap(_.prunePartitions(leftUnfilledRange))
              val filledDf =
                computeJoinPart(prunedLeft, joinPart, joinLevelBloomMapOpt, smallMode)
              // Cache join part data into intermediate table
              if (filledDf.isDefined) {
                logger.info(s"Writing to join part table: $partTable for partition range $unfilledRange")
                filledDf.get.save(partTable,
                                  tableProps,
                                  stats = prunedLeft.map(_.stats),
                                  sortByCols = joinPart.groupBy.keyColumns.toScala)
              }
            })
          val elapsedMins = (System.currentTimeMillis() - start) / 60000
          partMetrics.gauge(Metrics.Name.LatencyMinutes, elapsedMins)
          partMetrics.gauge(Metrics.Name.PartitionCount, partitionCount)
          logger.info(s"Wrote ${partitionCount} partitions to join part table: $partTable in $elapsedMins minutes")
        }
      } catch {
        case e: Exception =>
          logger.error(
            s"Error while processing groupBy: ${joinConf.metaData.name}/${joinPart.groupBy.getMetaData.getName}")
          throw e
      }
      if (tableUtils.tableExists(partTable)) {
        Some(tableUtils.sql(rightRange.genScanQuery(query = null, partTable)))
      } else {
        // Happens when everything is handled by bootstrap
        None
      }
    }
  }

  def computeJoinPart(leftDfWithStats: Option[DfWithStats],
                      joinPart: JoinPart,
                      joinLevelBloomMapOpt: Option[Map[String, BloomFilter]],
                      skipBloom: Boolean = false): Option[DataFrame] = {

    if (leftDfWithStats.isEmpty) {
      // happens when all rows are already filled by bootstrap tables
      logger.info(s"\nBackfill is NOT required for ${joinPart.groupBy.metaData.name} since all rows are bootstrapped.")
      return None
    }

    val leftDf = leftDfWithStats.get.df
    val rowCount = leftDfWithStats.get.count
    val unfilledRange = leftDfWithStats.get.partitionRange

    logger.info(
      s"\nBackfill is required for ${joinPart.groupBy.metaData.name} for $rowCount rows on range $unfilledRange")
    val rightBloomMap = if (skipBloom) {
      None
    } else {
      JoinUtils.genBloomFilterIfNeeded(leftDf,
                                       joinPart,
                                       joinConf,
                                       rowCount,
                                       unfilledRange,
                                       tableUtils,
                                       joinLevelBloomMapOpt)
    }
    val rightSkewFilter = joinConf.partSkewFilter(joinPart)
    def genGroupBy(partitionRange: PartitionRange) =
      GroupBy.from(joinPart.groupBy,
                   partitionRange,
                   tableUtils,
                   computeDependency = true,
                   rightBloomMap,
                   rightSkewFilter,
                   mutationScan = mutationScan,
                   showDf = showDf)

    // all lazy vals - so evaluated only when needed by each case.
    lazy val partitionRangeGroupBy = genGroupBy(unfilledRange)

    lazy val unfilledTimeRange = {
      val timeRange = leftDf.timeRange
      logger.info(s"left unfilled time range: $timeRange")
      timeRange
    }

    val leftSkewFilter = joinConf.skewFilter(Some(joinPart.rightToLeft.values.toSeq))
    // this is the second time we apply skew filter - but this filters only on the keys
    // relevant for this join part.
    lazy val skewFilteredLeft = leftSkewFilter
      .map { sf =>
        val filtered = leftDf.filter(sf)
        logger.info(s"""Skew filtering left-df for
                   |GroupBy: ${joinPart.groupBy.metaData.name}
                   |filterClause: $sf
                   |""".stripMargin)
        filtered
      }
      .getOrElse(leftDf)

    /*
      For the corner case when the values of the key mapping also exist in the keys, for example:
      Map(user -> user_name, user_name -> user)
      the below logic will first rename the conflicted column with some random suffix and update the rename map
     */
    lazy val renamedLeftDf = {
      val columns = skewFilteredLeft.columns.flatMap { column =>
        if (joinPart.leftToRight.contains(column)) {
          Some(col(column).as(joinPart.leftToRight(column)))
        } else if (joinPart.rightToLeft.contains(column)) {
          None
        } else {
          Some(col(column))
        }
      }
      skewFilteredLeft.select(columns: _*)
    }

    lazy val shiftedPartitionRange = unfilledTimeRange.toPartitionRange.shift(-1)
    val rightDf = (joinConf.left.dataModel, joinPart.groupBy.dataModel, joinPart.groupBy.inferredAccuracy) match {
      case (Entities, Events, _)   => partitionRangeGroupBy.snapshotEvents(unfilledRange)
      case (Entities, Entities, _) => partitionRangeGroupBy.snapshotEntities
      case (Events, Events, Accuracy.SNAPSHOT) =>
        genGroupBy(shiftedPartitionRange).snapshotEvents(shiftedPartitionRange)
      case (Events, Events, Accuracy.TEMPORAL) =>
        genGroupBy(unfilledTimeRange.toPartitionRange).temporalEvents(renamedLeftDf, Some(unfilledTimeRange))

      case (Events, Entities, Accuracy.SNAPSHOT) => genGroupBy(shiftedPartitionRange).snapshotEntities

      case (Events, Entities, Accuracy.TEMPORAL) => {
        // Snapshots and mutations are partitioned with ds holding data between <ds 00:00> and ds <23:59>.
        genGroupBy(shiftedPartitionRange).temporalEntities(renamedLeftDf)
      }
    }
    val rightDfWithDerivations = if (joinPart.groupBy.hasDerivations) {
      val finalOutputColumns = joinPart.groupBy.derivationsScala.finalOutputColumn(rightDf.columns).toSeq
      val result = rightDf.select(finalOutputColumns: _*)
      result
    } else {
      rightDf
    }
    if (showDf) {
      logger.info(s"printing results for joinPart: ${joinConf.metaData.name}::${joinPart.groupBy.metaData.name}")
      rightDfWithDerivations.prettyPrint()
    }
    Some(rightDfWithDerivations)
  }

  def computeRange(leftDf: DataFrame,
                   leftRange: PartitionRange,
                   bootstrapInfo: BootstrapInfo,
                   runSmallMode: Boolean = false): Option[DataFrame]

  def computeJoin(stepDays: Option[Int] = None, overrideStartPartition: Option[String] = None): DataFrame = {
    computeJoinOpt(stepDays, overrideStartPartition).get
  }

  def computeJoinOpt(stepDays: Option[Int] = None, overrideStartPartition: Option[String] = None): Option[DataFrame] = {

    assert(Option(joinConf.metaData.team).nonEmpty,
           s"join.metaData.team needs to be set for join ${joinConf.metaData.name}")

    joinConf.joinParts.asScala.foreach { jp =>
      assert(Option(jp.groupBy.metaData.team).nonEmpty,
             s"groupBy.metaData.team needs to be set for joinPart ${jp.groupBy.metaData.name}")
    }

    // Run validations before starting the job
    val today = tableUtils.partitionSpec.at(System.currentTimeMillis())
    val analyzer = new Analyzer(tableUtils, joinConf, today, today, silenceMode = true)
    try {
      analyzer.analyzeJoin(joinConf, validationAssert = true)
      metrics.gauge(Metrics.Name.validationSuccess, 1)
      logger.info("Join conf validation succeeded. No error found.")
    } catch {
      case ex: AssertionError =>
        metrics.gauge(Metrics.Name.validationFailure, 1)
        logger.error(s"Validation failed. Please check the validation error in log.")
        if (tableUtils.backfillValidationEnforced) throw ex
      case e: Throwable =>
        metrics.gauge(Metrics.Name.validationFailure, 1)
        logger.error(s"An unexpected error occurred during validation. ${e.getMessage}")
    }

    // First run command to archive tables that have changed semantically since the last run
    val archivedAtTs = Instant.now()
    tablesToRecompute(joinConf, outputTable, tableUtils).foreach(
      tableUtils.archiveOrDropTableIfExists(_, Some(archivedAtTs)))

    // detect holes and chunks to fill
    // OverrideStartPartition is used to replace the start partition of the join config. This is useful when
    //  1 - User would like to test run with different start partition
    //  2 - User has entity table which is cumulative and only want to run backfill for the latest partition
    val rangeToFill = JoinUtils.getRangesToFill(joinConf.left,
                                                tableUtils,
                                                endPartition,
                                                overrideStartPartition,
                                                joinConf.historicalBackfill)
    logger.info(s"Join range to fill $rangeToFill")
    val unfilledRanges = tableUtils
      .unfilledRanges(outputTable, rangeToFill, Some(Seq(joinConf.left.table)), skipFirstHole = skipFirstHole)
      .getOrElse(Seq.empty)

    def finalResult: DataFrame = tableUtils.sql(rangeToFill.genScanQuery(null, outputTable))
    if (unfilledRanges.isEmpty) {
      logger.info(s"\nThere is no data to compute based on end partition of ${rangeToFill.end}.\n\n Exiting..")
      return Some(finalResult)
    }

    stepDays.foreach(metrics.gauge("step_days", _))
    val stepRanges = unfilledRanges.flatMap { unfilledRange =>
      stepDays.map(unfilledRange.steps).getOrElse(Seq(unfilledRange))
    }

    val leftSchema = leftDf(joinConf, unfilledRanges.head, tableUtils, limit = Some(1)).map(df => df.schema)
    // build bootstrap info once for the entire job
    val bootstrapInfo = BootstrapInfo.from(joinConf, rangeToFill, tableUtils, leftSchema, mutationScan = mutationScan)

    val wholeRange = PartitionRange(unfilledRanges.minBy(_.start).start, unfilledRanges.maxBy(_.end).end)(tableUtils)

    val runSmallMode = {
      if (tableUtils.smallModelEnabled) {
        val thresholdCount =
          leftDf(joinConf, wholeRange, tableUtils, limit = Some(tableUtils.smallModeNumRowsCutoff + 1)).get.count()
        val result = thresholdCount <= tableUtils.smallModeNumRowsCutoff
        if (result) {
          logger.info(s"Counted $thresholdCount rows, running join in small mode.")
        } else {
          logger.info(
            s"Counted greater than ${tableUtils.smallModeNumRowsCutoff} rows, proceeding with normal computation.")
        }
        result
      } else {
        false
      }
    }

    val effectiveRanges = if (runSmallMode) {
      Seq(wholeRange)
    } else {
      stepRanges
    }

    logger.info(s"Join ranges to compute: ${effectiveRanges.map { _.toString }.pretty}")
    effectiveRanges.zipWithIndex.foreach {
      case (range, index) =>
        val startMillis = System.currentTimeMillis()
        val progress = s"| [${index + 1}/${effectiveRanges.size}]"
        logger.info(s"Computing join for range: ${range.toString}  $progress")
        leftDf(joinConf, range, tableUtils).map { leftDfInRange =>
          if (showDf) leftDfInRange.prettyPrint()
          // set autoExpand = true to ensure backward compatibility due to column ordering changes
          val finalDf = computeRange(leftDfInRange, range, bootstrapInfo, runSmallMode)
          if (selectedJoinParts.isDefined) {
            assert(finalDf.isEmpty,
                   "The arg `selectedJoinParts` is defined, so no final join is required. `finalDf` should be empty")
            logger.info(s"Skipping writing to the output table for range: ${range.toString}  $progress")
            return None
          } else {
            finalDf.get.save(outputTable, tableProps, autoExpand = true)
            val elapsedMins = (System.currentTimeMillis() - startMillis) / (60 * 1000)
            metrics.gauge(Metrics.Name.LatencyMinutes, elapsedMins)
            metrics.gauge(Metrics.Name.PartitionCount, range.partitions.length)
            logger.info(
              s"Wrote to table $outputTable, into partitions: ${range.toString} $progress in $elapsedMins mins")
          }
        }
    }
    logger.info(s"Wrote to table $outputTable, into partitions: $unfilledRanges")
    Some(finalResult)
  }
}
